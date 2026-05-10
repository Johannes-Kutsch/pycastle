import dataclasses
import json
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ..agent_output_protocol import (
    AgentOutputProtocolError,
    AgentRole,
    FailedOutput,
    PlannerOutput,
)
from ..agent_runner import AgentRunnerProtocol, RunRequest
from ..config import Config
from ..errors import AgentFailedError
from ..prompt_pipeline import PromptTemplate
from ..services import GitService
from ..services.github_service import GithubService
from ..status_display import StatusDisplay
from ..worktree import transient_worktree
from ._rows import phase_row
from .implement import branch_for
from .preflight import PreflightAFK, PreflightHITL, PreflightReady, ensure_preflight

if TYPE_CHECKING:
    pass


class _PlanningDeps(Protocol):
    cfg: Config
    status_display: StatusDisplay
    agent_runner: AgentRunnerProtocol
    repo_root: Path
    git_svc: GitService
    github_svc: GithubService
    preflight_verdict: PreflightReady | None


@dataclasses.dataclass(frozen=True)
class PlanReady:
    worktree_sha: str | None
    issues: list[dict]


@dataclasses.dataclass(frozen=True)
class AllBlocked:
    blocked: list[dict]


def hydrate_planned_issues(
    plan_result: "PlanReady", open_issues: list[dict]
) -> "PlanReady":
    by_number = {i["number"]: i for i in open_issues}
    hydrated: list[dict] = []
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
            }
        )
    return PlanReady(worktree_sha=plan_result.worktree_sha, issues=hydrated)


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

    async with phase_row(
        deps.status_display,
        "Plan",
        initial_phase="Planning",
        startup_message=startup_msg,
    ) as row:
        if _in_flight:
            nums = ", ".join(f"#{i['number']}" for i in _in_flight)
            row.close(
                f"resuming {len(_in_flight)} in-flight branch(es) ({nums}) labeled"
                f" {deps.cfg.issue_label}, skipping plan agent"
            )
            return PlanReady(worktree_sha=None, issues=_fill_fields(_in_flight))

        # Single-issue and multi-issue paths both need preflight + plan sandbox.
        # When uncached, pass sha=None — ensure_preflight will call get_head_sha
        # after pull, then checkout_detached the worktree to that sha.
        _pre_sha = (
            deps.preflight_verdict.sha if deps.preflight_verdict is not None else None
        )

        async with transient_worktree("plan-sandbox", sha=_pre_sha, deps=deps) as wt:
            verdict = await ensure_preflight(deps, wt)
            if isinstance(verdict, (PreflightHITL, PreflightAFK)):
                row.close(f"preflight gate blocked (issue #{verdict.issue_number})")
                return verdict
            sha = verdict.sha

            if len(open_issues) == 1:
                row.close(
                    f"only one open issue (#{open_issues[0]['number']}) labeled"
                    f" {deps.cfg.issue_label}, skipping plan agent"
                )
                return PlanReady(worktree_sha=sha, issues=_fill_fields(open_issues))

            try:
                output = await deps.agent_runner.run(
                    RunRequest(
                        name="Plan Agent",
                        template=PromptTemplate.PLAN,
                        mount_path=wt,
                        role=AgentRole.PLANNER,
                        scope_args={
                            "ALL_OPEN_ISSUES_JSON": json.dumps(all_open_issues),
                            "READY_FOR_AGENT_ISSUES_JSON": json.dumps(open_issues),
                        },
                        model=deps.cfg.plan_override.model,
                        effort=deps.cfg.plan_override.effort,
                        stage="plan-sandbox",
                        skip_preflight=True,
                        status_display=deps.status_display,
                        work_body=f"Creating Plan from {len(open_issues)} issues",
                    )
                )
            except AgentOutputProtocolError as exc:
                raise RuntimeError(str(exc)) from exc

            if isinstance(output, FailedOutput):
                raise AgentFailedError(
                    role_value=AgentRole.PLANNER.value,
                    worktree_path=wt,
                )
            if not isinstance(output, PlannerOutput):
                raise RuntimeError(
                    f"Planner returned unexpected output type: {type(output).__name__}"
                )
            if not output.issues:
                blocked_lines = [
                    f"  #{b['number']} blocked by #{b['blocked_by']}: {b['reason']}"
                    for b in output.blocked
                ]
                if blocked_lines:
                    row.close(
                        "\n".join(
                            ["All ready-for-agent issues are blocked:"] + blocked_lines
                        )
                    )
                else:
                    row.close("All ready-for-agent issues are blocked.")
                return AllBlocked(blocked=output.blocked)

            plan = PlanReady(
                worktree_sha=sha,
                issues=sorted(output.issues, key=lambda i: i["number"]),
            )
            hydrated = hydrate_planned_issues(plan, open_issues)
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
