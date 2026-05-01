import dataclasses
from typing import TypeAlias

from ..agent_output_protocol import AgentOutputProtocolError
from ._deps import Deps
from .plan import PlanAFK, PlanHITL, handle_preflight_failure, strip_stale_blocker_refs


@dataclasses.dataclass(frozen=True)
class PreflightReady:
    sha: str
    issues: list[dict]


PreflightResult: TypeAlias = PreflightReady | PlanHITL | PlanAFK


async def preflight_phase(deps: Deps) -> PreflightResult:
    sha = deps.git_svc.get_head_sha(deps.repo_root)
    open_issues = strip_stale_blocker_refs(
        deps.github_svc.get_open_issues(deps.cfg.issue_label)
    )
    if not open_issues:
        return PreflightReady(sha=sha, issues=[])

    worktree_path = (
        deps.repo_root / deps.cfg.pycastle_dir / ".worktrees" / "plan-sandbox"
    )
    deps.git_svc.checkout_detached(deps.repo_root, worktree_path, sha)

    try:
        failures = await deps.agent_runner.run_preflight(
            name="preflight-checks",
            mount_path=worktree_path,
            stage="plan-sandbox",
            status_display=deps.status_display,
        )

        if failures:
            try:
                verdict, pf_num = await handle_preflight_failure(
                    tuple(failures), deps, worktree_path
                )
            except AgentOutputProtocolError as parse_exc:
                raise RuntimeError(str(parse_exc)) from parse_exc
            if verdict == "hitl":
                return PlanHITL(worktree_sha=sha, issue_number=pf_num)
            pf_title = deps.github_svc.get_issue_title(pf_num)
            return PlanAFK(
                worktree_sha=sha, issues=[{"number": pf_num, "title": pf_title}]
            )

        return PreflightReady(sha=sha, issues=open_issues)
    finally:
        deps.git_svc.remove_worktree(deps.repo_root, worktree_path)
