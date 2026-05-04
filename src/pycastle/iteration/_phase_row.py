from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from ..status_display import StatusDisplay


class PhaseRow:
    def __init__(self, status_display: StatusDisplay, caller: str) -> None:
        self._status_display = status_display
        self._caller = caller
        self._closed = False

    def close(self, shutdown_message: str, shutdown_style: str = "success") -> None:
        if self._closed:
            return
        self._status_display.remove(self._caller, shutdown_message, shutdown_style)
        self._closed = True


@asynccontextmanager
async def phase_row(
    status_display: StatusDisplay,
    caller: str,
    initial_phase: str = "Setup",
    startup_message: str = "started",
) -> AsyncGenerator[PhaseRow, None]:
    status_display.register(caller, "phase", startup_message=startup_message, initial_phase=initial_phase)
    row = PhaseRow(status_display, caller)
    try:
        yield row
    finally:
        if not row._closed:
            status_display.remove(caller, "failed", shutdown_style="error")
