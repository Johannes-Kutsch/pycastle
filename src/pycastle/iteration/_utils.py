import asyncio

from ._deps import Deps


async def _wait_for_clean_working_tree(deps: Deps, caller: str, phase: str = "") -> None:
    if deps.git_svc.is_working_tree_clean(deps.repo_root):
        return
    phase_name = phase or caller.lower()
    deps.status_display.print(
        caller,
        "Working tree has uncommitted changes. "
        f"Please commit or revert all local changes before the {phase_name} phase can proceed.",
        style="error",
    )
    while not deps.git_svc.is_working_tree_clean(deps.repo_root):
        await asyncio.sleep(10)
