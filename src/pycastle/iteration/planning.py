import dataclasses
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
from ..prompts.scope_args import build_plan_scope_args
from ..services import GitService
from ..services.github_service import GithubService
from ..display.status_display import StatusDisplay
from ..infrastructure.worktree import transient_worktree
from ._rows import status_row
from .implement import branch_for
from .planning_issue_intake import (
    PlanReady,
    PreparedPlanningIssueSet,
    hydrate_planned_issues,
    planning_blocker_summary,
    prepare_planning_issue_set,
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


def _fill_fields(issues: list[dict]) -> list[dict]:
    return [
        {**i, "body": i.get("body") or "", "comments": i.get("comments") or []}
        for i in issues
    ]


async def planning_phase(
    deps: _PlanningDeps,
    open_issues: list[dict],
    all_open_issues: list[dict],
    prepared_issue_set: PreparedPlanningIssueSet | None = None,
    in_flight: list[dict] | None = None,
) -> PlanReady | AllBlocked | PreflightHITL | PreflightAFK:
    _in_flight = in_flight or []
    issue_set = (
        prepared_issue_set
        if prepared_issue_set is not None
        else prepare_planning_issue_set(open_issues, deps.cfg)
    )

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

        for action in issue_set.label_sync_actions:
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

        well_formed = list(issue_set.ready_candidates)
        readiness_by_number = dict(issue_set.ready_readiness_by_number)

        if not well_formed:
            blocker_summary = planning_blocker_summary(issue_set.blocker_summary_inputs)
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
                        scope_args=build_plan_scope_args(
                            all_open_issues=all_open_issues,
                            ready_for_agent_issues=well_formed,
                        ),
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
                    issue_set.blocker_summary_inputs
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

            plan_numbers = {issue["number"] for issue in output.issues}
            plan = PlanReady(
                issues=sorted(output.issues, key=lambda i: i["number"]),
                sha=sha,
                readiness_by_number={
                    issue_number: readiness
                    for issue_number, readiness in readiness_by_number.items()
                    if issue_number in plan_numbers
                },
            )
            ready_sources = [
                {**issue, "readiness": readiness_by_number[issue["number"]]}
                for issue in well_formed
            ]
            hydrated = hydrate_planned_issues(plan, ready_sources)
            if not hydrated.issues:
                blocker_summary = planning_blocker_summary(
                    issue_set.blocker_summary_inputs
                )
                lines = ["All ready-for-agent issues are blocked."]
                if blocker_summary:
                    lines.append(blocker_summary)
                row.close("\n".join(lines))
                return AllBlocked(blocked=[])
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
