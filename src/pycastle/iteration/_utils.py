import asyncio

from ._deps import Deps


async def _wait_for_clean_working_tree(deps: Deps, phase: str = "merge") -> None:
    if deps.git_svc.is_working_tree_clean(deps.repo_root):
        return
    deps.status_display.print(
        "[red]Working tree has uncommitted changes. "
        f"Please commit or revert all local changes before the {phase} phase can proceed.[/red]",
        source="working-tree-dirty",
    )
    while not deps.git_svc.is_working_tree_clean(deps.repo_root):
        await asyncio.sleep(10)
