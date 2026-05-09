import dataclasses
import json
from pathlib import Path
from typing import Protocol

from ..agent_output_protocol import AgentOutputProtocolError, AgentRole, PlannerOutput
from ..agent_result import PreflightFailure
from ..agent_runner import AgentRunnerProtocol, RunRequest
from ..config import Config
from ..prompt_pipeline import PromptTemplate
from ..services import GitService
from ..status_display import StatusDisplay
from ..worktree import transient_worktree


class _PlanningDeps(Protocol):
    cfg: Config
    status_display: StatusDisplay
    agent_runner: AgentRunnerProtocol
    repo_root: Path
    git_svc: GitService


@dataclasses.dataclass(frozen=True)
class PlanReady:
    worktree_sha: str
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


async def planning_phase(
    deps: _PlanningDeps, sha: str, open_issues: list[dict], all_open_issues: list[dict]
) -> PlanReady | AllBlocked:
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

        if isinstance(output, PreflightFailure):
            raise RuntimeError("Planner returned a PreflightFailure unexpectedly")

        if not isinstance(output, PlannerOutput):
            raise RuntimeError(
                f"Planner returned unexpected output type: {type(output).__name__}"
            )
        if not output.issues:
            return AllBlocked(blocked=output.blocked)
        return PlanReady(
            worktree_sha=sha,
            issues=sorted(output.issues, key=lambda i: i["number"]),
        )
