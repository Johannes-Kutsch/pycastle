import dataclasses
from typing import Literal, TypeAlias

ImproveMode: TypeAlias = Literal["until_sleep", "endless"] | None


@dataclasses.dataclass(frozen=True)
class Done:
    pass


def should_dispatch_improve(
    improve_mode: ImproveMode,
    slept_once: bool,
    improve_dispatched_this_iteration: bool,
) -> bool:
    if improve_mode is None:
        return False
    if improve_dispatched_this_iteration:
        return False
    if improve_mode == "until_sleep" and slept_once:
        return False
    return True
