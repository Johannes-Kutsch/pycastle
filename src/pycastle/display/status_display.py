import sys
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pycastle.display.status_print_sequencing import (
    Kind,
    OutputEvent,
    StatusPrintSequencer,
)

WORK_PHASE = "Work"


@dataclass(frozen=True)
class ModelDisplayMetadata:
    service: str
    model: str
    effort: str


@runtime_checkable
class SimpleStatusDisplay(Protocol):
    def register(
        self,
        caller: str,
        kind: Kind,
        startup_message: str = "started",
    ) -> None: ...
    def remove(
        self,
        caller: str,
        shutdown_message: str = "finished",
    ) -> None: ...
    def print(self, caller: str, message: object) -> None: ...


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
        del work_body, initial_phase, color_key, model_display
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
        del shutdown_style
        self.print(caller, shutdown_message)
        self._sequencer.remove_caller(caller, preserve_last_output_kind=True)

    def print(self, caller: str, message: object, style: str | None = None) -> None:
        del style
        rendered = str(message)
        lines = rendered.split("\n")
        if self._sequencer.record_output_event(
            OutputEvent(caller=caller, text=rendered)
        ):
            sys.stdout.write("\n")
        for line in lines:
            if caller:
                sys.stdout.write(f"[{caller}] {line}\n")
            else:
                sys.stdout.write(f"{line}\n")
