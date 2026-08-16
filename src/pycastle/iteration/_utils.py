import asyncio
from pathlib import Path
from typing import Protocol

from pycastle.config import Config
from pycastle.display.status_display import StatusDisplay
from pycastle.iteration.branch_resolution import find_checked_out_worktrees
from pycastle.services import GitService


class _UtilDeps(Protocol):
    git_svc: GitService
    repo_root: Path
    status_display: StatusDisplay
    cfg: Config


async def _wait_for_clean_working_tree(deps: _UtilDeps, caller: str) -> None:
    if deps.git_svc.is_working_tree_clean(deps.repo_root):
        return
    deps.status_display.print(
        caller,
        "Working tree has uncommitted changes. "
        f"Please commit or revert all local changes before the {caller.lower()} phase can proceed.",
        style="error",
    )
    while not deps.git_svc.is_working_tree_clean(deps.repo_root):  # noqa: ASYNC110  # polling is necessary; no event source for working-tree clean
        await asyncio.sleep(10)


async def _wait_for_operating_branch_release(deps: _UtilDeps, caller: str) -> None:
    """Wait until the operating branch is not checked out in any worktree.

    Git refuses to fetch into a checked-out branch regardless of working-tree cleanliness.
    The checkout is the blocking condition; dirty files in the repo root are not.
    """
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
