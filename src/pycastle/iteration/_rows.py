from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Literal

from ..errors import AgentTimeoutError, UsageLimitError
from ..display.status_display import ModelDisplayMetadata, StatusDisplay


class StatusRow:
    def __init__(self, status_display: StatusDisplay, caller: str) -> None:
        self._status_display = status_display
        self._caller = caller
        self._closed = False

    @property
    def is_closed(self) -> bool:
        return self._closed

    def close(self, shutdown_message: str, shutdown_style: str = "success") -> None:
        if self._closed:
            return
        self._status_display.remove(self._caller, shutdown_message, shutdown_style)
        self._closed = True


@asynccontextmanager
async def status_row(
    status_display: StatusDisplay,
    caller: str,
    *,
    kind: Literal["phase", "agent"],
    must_close: bool,
    color_key: int | None = None,
    work_body: str = "",
    initial_phase: str = "Setup",
    startup_message: str = "started",
    model_display: ModelDisplayMetadata | None = None,
) -> AsyncGenerator[StatusRow, None]:
    status_display.register(
        caller,
        kind,
        startup_message=startup_message,
        work_body=work_body,
        initial_phase=initial_phase,
        color_key=color_key,
        model_display=model_display,
    )
    row = StatusRow(status_display, caller)
    try:
        yield row
    except UsageLimitError:
        row.close("usage limit reached", shutdown_style="interrupted")
        raise
    except AgentTimeoutError:
        row.close("timed out", shutdown_style="interrupted")
        raise
    except BaseException:
        row.close("failed", shutdown_style="error")
        raise
    else:
        if not row.is_closed:
            if must_close:
                row.close("failed", shutdown_style="error")
            else:
                row.close("finished")
