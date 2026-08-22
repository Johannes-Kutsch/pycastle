import asyncio
from pathlib import Path
from typing import Protocol

from pycastle.config import Config
from pycastle.display.status_display import StatusDisplay
from pycastle.iteration.branch_resolution import find_checked_out_worktrees
from pycastle.services import GitService
from pycastle.services.git_service import OperatingBranchCheckedOutError


class _UtilDeps(Protocol):
    git_svc: GitService
    repo_root: Path
    status_display: StatusDisplay
    cfg: Config


async def _wait_for_operating_branch_release(deps: _UtilDeps, caller: str) -> None:
    branch = deps.cfg.operating_branch
    blocking = find_checked_out_worktrees(
        branch, deps.git_svc.list_worktrees_with_branches(deps.repo_root)
    )
    if not blocking:
        return
    path_str = ", ".join(str(p) for p in blocking)
    deps.status_display.print(
        caller,
        f"Operating branch {branch!r} is checked out at {path_str}. "
        f"The {caller.lower()} phase is waiting for it to be released.",
        style="error",
    )
    while True:  # polling is necessary; no event source for worktree checkout changes
        await asyncio.sleep(10)
        blocking = find_checked_out_worktrees(
            branch, deps.git_svc.list_worktrees_with_branches(deps.repo_root)
        )
        if not blocking:
            return


async def _advance_branch_ref_through_gate(
    deps: _UtilDeps,
    caller: str,
    target: str,
    source: str,
) -> None:
    """Advance target ref to source, re-entering the checkout gate on conflict."""
    while True:
        try:
            deps.git_svc.advance_branch_ref(deps.repo_root, target, source)
        except OperatingBranchCheckedOutError:
            await _wait_for_operating_branch_release(deps, caller)
        else:
            return
