import builtins
from typing import Protocol, runtime_checkable


@runtime_checkable
class StatusDisplay(Protocol):
    def register(self, caller: str, startup_message: str = "started", work_body: str = "", initial_phase: str = "Setup") -> None: ...
    def update_phase(self, name: str, phase: str) -> None: ...
    def reset_idle_timer(self, name: str) -> None: ...
    def remove(self, caller: str, shutdown_message: str = "finished", shutdown_style: str = "success") -> None: ...
    def print(self, caller: str, message: object, style: str | None = None) -> None: ...


class PlainStatusDisplay:
    def __init__(self) -> None:
        self._last_caller: str | None = None

    def _blank_before(self, caller: str) -> bool:
        return caller != self._last_caller or caller == ""

    def register(self, caller: str, startup_message: str = "started", work_body: str = "", initial_phase: str = "Setup") -> None:
        self.print(caller, startup_message)

    def update_phase(self, name: str, phase: str) -> None:
        pass

    def reset_idle_timer(self, name: str) -> None:
        pass

    def remove(self, caller: str, shutdown_message: str = "finished", shutdown_style: str = "success") -> None:
        self.print(caller, shutdown_message)

    def print(self, caller: str, message: object, style: str | None = None) -> None:
        if self._blank_before(caller):
            builtins.print()
        if caller:
            builtins.print(f"[{caller}] {message}")
        else:
            builtins.print(message)
        self._last_caller = caller
