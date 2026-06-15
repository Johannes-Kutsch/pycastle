from dataclasses import dataclass
import builtins
from typing import Protocol, runtime_checkable

from .status_print_sequencing import Kind, StatusPrintSequencer


@dataclass(frozen=True)
class ModelDisplayMetadata:
    service: str
    model: str
    effort: str


@runtime_checkable
class StatusDisplay(Protocol):
    def register(
        self,
        caller: str,
        kind: Kind,
        startup_message: str = "started",
        work_body: str = "",
        initial_phase: str = "Setup",
        color_key: int | None = None,
        model_display: ModelDisplayMetadata | None = None,
    ) -> None: ...
    def update_phase(self, name: str, phase: str) -> None: ...
    def reset_idle_timer(self, name: str) -> None: ...
    def update_tokens(self, name: str, current_tokens: int) -> None: ...
    def remove(
        self,
        caller: str,
        shutdown_message: str = "finished",
        shutdown_style: str = "success",
    ) -> None: ...
    def print(self, caller: str, message: object, style: str | None = None) -> None: ...


class PlainStatusDisplay:
    def __init__(self) -> None:
        self._sequencer = StatusPrintSequencer()

    def register(
        self,
        caller: str,
        kind: Kind,
        startup_message: str = "started",
        work_body: str = "",
        initial_phase: str = "Setup",
        color_key: int | None = None,
        model_display: ModelDisplayMetadata | None = None,
    ) -> None:
        self._sequencer.register_caller(caller, kind)
        self.print(caller, startup_message)

    def update_phase(self, name: str, phase: str) -> None:
        pass

    def reset_idle_timer(self, name: str) -> None:
        pass

    def update_tokens(self, name: str, current_tokens: int) -> None:
        pass

    def remove(
        self,
        caller: str,
        shutdown_message: str = "finished",
        shutdown_style: str = "success",
    ) -> None:
        self.print(caller, shutdown_message)
        self._sequencer.remove_caller(caller, preserve_last_output_kind=True)

    def print(self, caller: str, message: object, style: str | None = None) -> None:
        lines = str(message).split("\n")
        if self._sequencer.should_prepend_blank_line(caller):
            builtins.print()
        self._sequencer.record_output(caller)
        for line in lines:
            if caller:
                builtins.print(f"[{caller}] {line}")
            else:
                builtins.print(line)
