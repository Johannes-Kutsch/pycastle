import json

from ..agent_output_protocol import AgentOutputProtocolError, AgentRole, parse
from ..agent_result import PreflightFailure
from ._deps import Deps
from .plan import PlanReady


async def planning_phase(deps: Deps, sha: str, open_issues: list[dict]) -> PlanReady:
    worktree_path = (
        deps.repo_root / deps.cfg.pycastle_dir / ".worktrees" / "plan-sandbox"
    )
    deps.git_svc.checkout_detached(deps.repo_root, worktree_path, sha)

    try:
        raw = await deps.agent_runner.run(
            name="Planner",
            prompt_file=deps.cfg.prompts_dir / "plan-prompt.md",
            mount_path=worktree_path,
            prompt_args={"OPEN_ISSUES_JSON": json.dumps(open_issues)},
            model=deps.cfg.plan_override.model,
            effort=deps.cfg.plan_override.effort,
            stage="plan-sandbox",
            skip_preflight=True,
            status_display=deps.status_display,
        )

        if isinstance(raw, PreflightFailure):
            raise RuntimeError("Planner returned a PreflightFailure unexpectedly")

        try:
            planner_output = parse(raw, AgentRole.PLANNER)
        except AgentOutputProtocolError as exc:
            raise RuntimeError(str(exc)) from exc

        return PlanReady(
            worktree_sha=sha,
            issues=sorted(planner_output.issues, key=lambda i: i["number"]),
        )
    finally:
        deps.git_svc.remove_worktree(deps.repo_root, worktree_path)
