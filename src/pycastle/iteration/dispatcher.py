import dataclasses
from typing import Literal, TypeAlias

ImproveMode: TypeAlias = Literal["until_sleep", "endless"] | None


@dataclasses.dataclass(frozen=True)
class RunPlan:
    pass


@dataclasses.dataclass(frozen=True)
class RunImplementDirect:
    pass


@dataclasses.dataclass(frozen=True)
class Done:
    pass


IterationAction: TypeAlias = RunPlan | RunImplementDirect | Done


def decide_iteration_action(
    open_afk_count: int,
    in_flight_count: int,
) -> IterationAction:
    if in_flight_count > 0:
        return RunImplementDirect()
    if open_afk_count >= 1:
        return RunPlan()
    return Done()


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
