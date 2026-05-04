import asyncio
from pathlib import Path
from typing import Protocol

from ..services import GitService
from ..status_display import StatusDisplay


class _UtilDeps(Protocol):
    git_svc: GitService
    repo_root: Path
    status_display: StatusDisplay


async def _wait_for_clean_working_tree(deps: _UtilDeps, caller: str) -> None:
    if deps.git_svc.is_working_tree_clean(deps.repo_root):
        return
    deps.status_display.print(
        caller,
        "Working tree has uncommitted changes. "
        f"Please commit or revert all local changes before the {caller.lower()} phase can proceed.",
        style="error",
    )
    while not deps.git_svc.is_working_tree_clean(deps.repo_root):
        await asyncio.sleep(10)
