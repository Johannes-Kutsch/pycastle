from dataclasses import dataclass
from typing import Literal

Kind = Literal["phase", "agent"]


@dataclass(frozen=True)
class OutputEvent:
    caller: str
    text: str


class StatusPrintSequencer:
    def __init__(self) -> None:
        self._last_caller: str | None = None
        self._last_kind: Kind | None = None
        self._kinds: dict[str, Kind] = {}

    def register_caller(self, caller: str, kind: Kind) -> None:
        if caller != "":
            self._kinds[caller] = kind

    def caller_kinds(self, callers: list[str]) -> dict[str, Kind | None]:
        return {caller: self.caller_kind(caller) for caller in callers}

    def remove_caller(
        self, caller: str, *, preserve_last_output_kind: bool = False
    ) -> None:
        self._kinds.pop(caller, None)
        if self._last_caller == caller and not preserve_last_output_kind:
            self._last_kind = None

    def caller_kind(self, caller: str) -> Kind | None:
        return self._kinds.get(caller)

    def should_prepend_blank_line(self, caller: str) -> bool:
        if caller == "":
            return True
        if caller == self._last_caller:
            return False
        kinds = {self._last_kind, self.caller_kind(caller)}
        if "agent" in kinds and kinds <= {"phase", "agent"}:
            return False
        return True

    def record_output_event(self, event: str | OutputEvent) -> bool:
        caller = event if isinstance(event, str) else event.caller
        should_prepend_blank_line = self.should_prepend_blank_line(caller)
        self.record_output(caller)
        return should_prepend_blank_line

    def record_output(self, caller: str) -> None:
        self._last_caller = caller
        self._last_kind = self.caller_kind(caller)
