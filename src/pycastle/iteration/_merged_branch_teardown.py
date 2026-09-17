import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from pycastle.display.status_display import StatusDisplay
from pycastle.infrastructure.worktree import (
    cleanup_durable_issue_worktree_after_success,
    worktree_identity,
)
from pycastle.services import GitCommandError, GitService


class _CfgLike(Protocol):
    operating_branch: str


class _TeardownDeps(Protocol):
    git_svc: GitService
    repo_root: Path
    cfg: _CfgLike
    status_display: StatusDisplay


async def teardown_merged_branch(
    branch: str,
    deps: _TeardownDeps,
    *,
    on_progress: Callable[[], None] | None = None,
) -> str | None:
    try:
        if not deps.git_svc.is_ancestor(
            branch, deps.repo_root, deps.cfg.operating_branch
        ):
            return None

        registered_worktrees = deps.git_svc.list_worktrees(deps.repo_root)
        worktree_path = worktree_identity(branch, deps.repo_root).path

        if worktree_path in registered_worktrees or worktree_path.exists():
            try:
                await asyncio.to_thread(
                    cleanup_durable_issue_worktree_after_success,
                    deps.git_svc,
                    deps.repo_root,
                    worktree_path,
                )
            except (GitCommandError, OSError) as e:
                deps.status_display.print(
                    "Merge",
                    f"Warning: could not remove worktree for {branch!r}: {e}",
                    "warning",
                )

        try:
            await asyncio.to_thread(deps.git_svc.delete_branch, branch, deps.repo_root)
        except GitCommandError as e:
            deps.status_display.print(
                "Merge",
                f"Warning: could not delete branch {branch!r}: {e}",
                "warning",
            )
            return None

        return branch
    finally:
        if on_progress is not None:
            on_progress()
