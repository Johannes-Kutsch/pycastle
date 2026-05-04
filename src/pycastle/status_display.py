import builtins
from typing import Literal, Protocol, runtime_checkable

Kind = Literal["phase", "agent"]


@runtime_checkable
class StatusDisplay(Protocol):
    def register(self, caller: str, kind: Kind, startup_message: str = "started", work_body: str = "", initial_phase: str = "Setup") -> None: ...
    def update_phase(self, name: str, phase: str) -> None: ...
    def reset_idle_timer(self, name: str) -> None: ...
    def remove(self, caller: str, shutdown_message: str = "finished", shutdown_style: str = "success") -> None: ...
    def print(self, caller: str, message: object, style: str | None = None) -> None: ...


class PlainStatusDisplay:
    def __init__(self) -> None:
        self._last_caller: str | None = None
        self._last_kind: str | None = None
        self._kinds: dict[str, str] = {}

    def _blank_before(self, caller: str) -> bool:
        if caller == "":
            return True
        if caller == self._last_caller:
            return False
        cur_kind = self._kinds.get(caller)
        if {self._last_kind, cur_kind} == {"phase", "agent"}:
            return False
        return True

    def register(self, caller: str, kind: Kind, startup_message: str = "started", work_body: str = "", initial_phase: str = "Setup") -> None:
        if caller != "":
            self._kinds[caller] = kind
        self.print(caller, startup_message)

    def update_phase(self, name: str, phase: str) -> None:
        pass

    def reset_idle_timer(self, name: str) -> None:
        pass

    def remove(self, caller: str, shutdown_message: str = "finished", shutdown_style: str = "success") -> None:
        self.print(caller, shutdown_message)
        self._kinds.pop(caller, None)

    def print(self, caller: str, message: object, style: str | None = None) -> None:
        lines = str(message).split("\n")
        if self._blank_before(caller):
            builtins.print()
        self._last_caller = caller
        self._last_kind = self._kinds.get(caller)
        for line in lines:
            if caller:
                builtins.print(f"[{caller}] {line}")
            else:
                builtins.print(line)
