import builtins
from typing import Protocol, runtime_checkable

_MISSING = object()


@runtime_checkable
class StatusDisplay(Protocol):
    def register(self, caller: str, startup_message: str = "started", work_body: str = "") -> None: ...
    def update_phase(self, name: str, phase: str) -> None: ...
    def reset_idle_timer(self, name: str) -> None: ...
    def remove(self, caller: str, shutdown_message: str = "finished", shutdown_style: str = "success") -> None: ...
    def print(self, caller: str, message: object, style: str | None = None) -> None: ...


class PlainStatusDisplay:
    def __init__(self) -> None:
        self._last_caller: str | None = None

    # ── new caller-based API ──────────────────────────────────────────────────

    def register(self, caller: str, startup_message: str = "started", work_body: str = "") -> None:
        if caller:
            builtins.print(f"[{caller}] {startup_message}")
        else:
            builtins.print(startup_message)

    def update_phase(self, name: str, phase: str) -> None:
        pass

    def reset_idle_timer(self, name: str) -> None:
        pass

    def remove(self, caller: str, shutdown_message: str = "finished", shutdown_style: str = "success") -> None:
        if caller:
            builtins.print(f"[{caller}] {shutdown_message}")
        else:
            builtins.print(shutdown_message)

    def print(self, caller_or_message: object, message: object = _MISSING, *, source: str = "", style: str | None = None) -> None:  # type: ignore[override]
        if message is _MISSING:
            # legacy signature: print(message, *, source="")
            builtins.print(caller_or_message)
        else:
            # new signature: print(caller, message, style=None)
            caller = str(caller_or_message)
            if self._last_caller is not None and caller != self._last_caller:
                builtins.print()
            if caller:
                builtins.print(f"[{caller}] {message}")
            else:
                builtins.print(message)
            self._last_caller = caller

    # ── legacy API kept for call sites not yet migrated ───────────────────────

    def add_agent(self, name: str, phase: str, work_body: str = "") -> None:
        pass

    def remove_agent(self, name: str) -> None:
        pass
