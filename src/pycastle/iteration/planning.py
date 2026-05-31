import dataclasses
import json
from pathlib import Path
from typing import Protocol

from ..agents.output_protocol import (
    AgentOutputProtocolError,
    AgentRole,
    PlannerOutput,
)
from ..agents.runner import AgentRunnerProtocol, RunRequest
from ..config import Config
from ..prompts.pipeline import PromptTemplate
from ..services import GitService
from ..services.github_service import GithubService
from ..display.status_display import StatusDisplay
from ..issue_readiness import IssueReadiness
from ..infrastructure.worktree import transient_worktree
from ._rows import status_row
from .implement import branch_for
from .planning_readiness import (
    evaluate_planning_readiness,
    planning_blocker_summary,
)
from .preflight import PreflightAFK, PreflightCache, PreflightHITL


class _PlanningDeps(Protocol):
    cfg: Config
    status_display: StatusDisplay
    agent_runner: AgentRunnerProtocol
    repo_root: Path
    git_svc: GitService
    github_svc: GithubService
    preflight_cache: PreflightCache


@dataclasses.dataclass(frozen=True)
class PlanReady:
    issues: list[dict]
    sha: str | None
    readiness_by_number: dict[int, IssueReadiness] = dataclasses.field(
        default_factory=dict
    )


@dataclasses.dataclass(frozen=True)
class AllBlocked:
    blocked: list[dict]


def _hydrate_blocked_issues(blocked: list[dict], open_issues: list[dict]) -> list[dict]:
    titles_by_number = {issue["number"]: issue["title"] for issue in open_issues}
    hydrated: list[dict] = []
    for blocked_issue in blocked:
        number = blocked_issue["number"]
        title = titles_by_number.get(number) or blocked_issue.get("title")
        if title is None:
            hydrated.append({"number": number})
            continue
        hydrated.append({"number": number, "title": title})
    return hydrated


def hydrate_planned_issues(
    plan_result: "PlanReady", open_issues: list[dict]
) -> "PlanReady":
    by_number = {i["number"]: i for i in open_issues}
    hydrated: list[dict] = []
    readiness_by_number = dict(plan_result.readiness_by_number)
    for issue in plan_result.issues:
        source = by_number.get(issue["number"])
        if source is None:
            raise RuntimeError(
                f"Planner returned issue #{issue['number']} which is not in the "
                f"ready-for-agent open issues list"
            )
        hydrated.append(
            {
                **issue,
                "body": source.get("body") or "",
                "comments": source.get("comments") or [],
                "labels": source.get("labels") or [],
            }
        )
        readiness = source.get("readiness")
        if isinstance(readiness, IssueReadiness):
            readiness_by_number[issue["number"]] = readiness
    return PlanReady(
        issues=hydrated,
        sha=plan_result.sha,
        readiness_by_number=readiness_by_number,
    )


def _fill_fields(issues: list[dict]) -> list[dict]:
    return [
        {**i, "body": i.get("body") or "", "comments": i.get("comments") or []}
        for i in issues
    ]


async def planning_phase(
    deps: _PlanningDeps,
    open_issues: list[dict],
    all_open_issues: list[dict],
    in_flight: list[dict] | None = None,
) -> PlanReady | AllBlocked | PreflightHITL | PreflightAFK:
    _in_flight = in_flight or []

    if _in_flight:
        startup_msg = f"checking {len(_in_flight)} in-flight branch(es) labeled {deps.cfg.issue_label}"
    else:
        startup_msg = f"started planning for {len(open_issues)} issue(s) labeled {deps.cfg.issue_label}"

    async with status_row(
        deps.status_display,
        "Plan",
        kind="phase",
        must_close=True,
        initial_phase="Planning",
        startup_message=startup_msg,
    ) as row:
        if _in_flight:
            verdict = await deps.preflight_cache.get_safe_sha(deps)
            if isinstance(verdict, (PreflightHITL, PreflightAFK)):
                row.close(f"preflight gate blocked (issue #{verdict.issue_number})")
                return verdict
            nums = ", ".join(f"#{i['number']}" for i in _in_flight)
            row.close(
                f"resuming {len(_in_flight)} in-flight branch(es) ({nums}) labeled"
                f" {deps.cfg.issue_label}, skipping plan agent"
            )
            return PlanReady(issues=_fill_fields(_in_flight), sha=verdict.sha)

        verdict = await deps.preflight_cache.get_safe_sha(deps)
        if isinstance(verdict, (PreflightHITL, PreflightAFK)):
            row.close(f"preflight gate blocked (issue #{verdict.issue_number})")
            return verdict
        sha = verdict.sha

        readiness_result = evaluate_planning_readiness(open_issues, deps.cfg)
        for action in readiness_result.label_sync_actions:
            if action.intent == "add":
                deps.github_svc.add_label_to_issue(
                    action.issue_number, action.label_name
                )
                if action.comment_body is not None:
                    deps.github_svc.post_comment(
                        action.issue_number, action.comment_body
                    )
                continue
            deps.github_svc.remove_label_from_issue(
                action.issue_number, action.label_name
            )

        well_formed = list(readiness_result.ready_candidates)
        readiness_by_number = dict(readiness_result.ready_readiness_by_number)

        if not well_formed:
            blocker_summary = planning_blocker_summary(
                readiness_result.blocker_summary_inputs
            )
            lines = ["All ready-for-agent issues are blocked."]
            if blocker_summary:
                lines.append(blocker_summary)
            row.close("\n".join(lines))
            return AllBlocked(blocked=[])

        if len(well_formed) == 1:
            row.close(
                f"only one open issue (#{well_formed[0]['number']}) labeled"
                f" {deps.cfg.issue_label}, skipping plan agent"
            )
            return PlanReady(
                issues=_fill_fields(well_formed),
                sha=sha,
                readiness_by_number=readiness_by_number,
            )

        async with transient_worktree("plan-sandbox", sha=sha, deps=deps) as wt:
            try:
                output = await deps.agent_runner.run(
                    RunRequest(
                        name="Plan Agent",
                        template=PromptTemplate.PLAN,
                        mount_path=wt,
                        role=AgentRole.PLANNER,
                        scope_args={
                            "ALL_OPEN_ISSUES_JSON": json.dumps(all_open_issues),
                            "READY_FOR_AGENT_ISSUES_JSON": json.dumps(well_formed),
                        },
                        model=deps.cfg.plan_override.model,
                        effort=deps.cfg.plan_override.effort,
                        service=deps.cfg.plan_override.service,
                        stage="plan-sandbox",
                        status_display=deps.status_display,
                        work_body=f"Creating Plan from {len(well_formed)} issues",
                    )
                )
            except AgentOutputProtocolError as exc:
                raise RuntimeError(str(exc)) from exc

            if not isinstance(output, PlannerOutput):
                raise RuntimeError(
                    f"Planner returned unexpected output type: {type(output).__name__}"
                )
            if not output.issues:
                blocked = _hydrate_blocked_issues(output.blocked, well_formed)
                blocker_summary = planning_blocker_summary(
                    readiness_result.blocker_summary_inputs
                )
                blocked_lines = [
                    _format_blocked_issue_line(blocked_issue)
                    for blocked_issue in blocked
                ]
                lines = [
                    "All ready-for-agent issues are blocked:"
                    if blocked_lines
                    else "All ready-for-agent issues are blocked."
                ]
                if blocker_summary:
                    lines.append(blocker_summary)
                lines.extend(blocked_lines)
                row.close("\n".join(lines))
                return AllBlocked(blocked=blocked)

            plan = PlanReady(
                issues=sorted(output.issues, key=lambda i: i["number"]),
                sha=sha,
                readiness_by_number=readiness_by_number,
            )
            ready_sources = [
                {**issue, "readiness": readiness_by_number[issue["number"]]}
                for issue in well_formed
            ]
            hydrated = hydrate_planned_issues(plan, ready_sources)
            issue_lines = [
                f"  #{i['number']}: {i['title']} → {branch_for(i['number'])}"
                for i in hydrated.issues
            ]
            row.close(
                "\n".join(
                    [
                        f"Planning complete, implementing {len(hydrated.issues)} issue(s):"
                    ]
                    + issue_lines
                )
            )
            return hydrated


def _format_blocked_issue_line(blocked_issue: dict) -> str:
    number = blocked_issue["number"]
    if "title" in blocked_issue:
        return f"  #{number}: {blocked_issue['title']}"
    return f"  #{number}"
