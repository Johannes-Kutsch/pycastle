import dataclasses
import json

from ..agent_output_protocol import AgentOutputProtocolError, AgentRole, PlannerOutput
from ..agent_result import PreflightFailure
from ..agent_runner import RunRequest
from ..worktree import detached_worktree
from ._deps import Deps


@dataclasses.dataclass(frozen=True)
class PlanReady:
    worktree_sha: str
    issues: list[dict]


async def planning_phase(deps: Deps, sha: str, open_issues: list[dict]) -> PlanReady:
    async with detached_worktree("plan-sandbox", sha, deps) as wt:
        try:
            output = await deps.agent_runner.run(
                RunRequest(
                    name="Plan Agent",
                    prompt_file=deps.cfg.prompts_dir / "plan-prompt.md",
                    mount_path=wt,
                    role=AgentRole.PLANNER,
                    prompt_args={"OPEN_ISSUES_JSON": json.dumps(open_issues)},
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
        return PlanReady(
            worktree_sha=sha,
            issues=sorted(output.issues, key=lambda i: i["number"]),
        )
