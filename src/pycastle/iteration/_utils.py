import asyncio

from ._deps import Deps


async def _wait_for_clean_working_tree(deps: Deps) -> None:
    if deps.git_svc.is_working_tree_clean(deps.repo_root):
        return
    deps.status_display.print(
        "[red]Working tree has uncommitted changes. "
        "Please commit or revert all local changes before the merge phase can proceed.[/red]"
    )
    while not deps.git_svc.is_working_tree_clean(deps.repo_root):
        await asyncio.sleep(10)
