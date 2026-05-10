import dataclasses
from typing import Literal, TypeAlias

ImproveMode: TypeAlias = Literal["until_sleep", "endless"] | None


@dataclasses.dataclass(frozen=True)
class Done:
    improve_cap_reached: bool = False


def should_dispatch_improve(
    improve_mode: ImproveMode,
    slept_once: bool,
    dispatched_count: int,
    improve_max: int | None,
) -> bool:
    if improve_mode is None:
        return False
    if improve_mode == "until_sleep" and slept_once:
        return False
    if improve_max is not None and dispatched_count >= improve_max:
        return False
    return True
