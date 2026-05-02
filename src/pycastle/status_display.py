import builtins
from typing import Protocol, runtime_checkable


@runtime_checkable
class StatusDisplay(Protocol):
    def add_agent(self, name: str, phase: str, work_body: str = "") -> None: ...
    def update_phase(self, name: str, phase: str) -> None: ...
    def remove_agent(self, name: str) -> None: ...
    def reset_idle_timer(self, name: str) -> None: ...
    def print(self, message: object, *, source: str = "") -> None: ...


class PlainStatusDisplay:
    def add_agent(self, name: str, phase: str, work_body: str = "") -> None:
        pass

    def update_phase(self, name: str, phase: str) -> None:
        pass

    def remove_agent(self, name: str) -> None:
        pass

    def reset_idle_timer(self, name: str) -> None:
        pass

    def print(self, message: object, *, source: str = "") -> None:
        builtins.print(message)
