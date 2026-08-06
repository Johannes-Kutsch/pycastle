from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

from pycastle.display.status_display import ModelDisplayMetadata, StatusDisplay
from pycastle.errors import AgentTimeoutError, UsageLimitError


@dataclass(frozen=True)
class StatusRowConfig:
    color_key: int | None = None
    work_body: str = ""
    initial_phase: str = "Setup"
    startup_message: str = "started"
    model_display: ModelDisplayMetadata | None = None


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
    config: StatusRowConfig | None = None,
) -> AsyncGenerator[StatusRow, None]:
    _cfg = config or StatusRowConfig()
    status_display.register(
        caller,
        kind,
        startup_message=_cfg.startup_message,
        work_body=_cfg.work_body,
        initial_phase=_cfg.initial_phase,
        color_key=_cfg.color_key,
        model_display=_cfg.model_display,
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
