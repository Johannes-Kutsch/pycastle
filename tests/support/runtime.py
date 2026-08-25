"""Shared test helpers for RuntimeInvocationDependencies construction."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from pycastle.display.rows import StatusRowConfig, status_row
from pycastle.display.status_display import PlainStatusDisplay
from pycastle.execution_contracts import RuntimeStatusDisplay, RuntimeStatusRowConfig

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager


def plain_status_display_factory() -> RuntimeStatusDisplay:
    return cast("RuntimeStatusDisplay", PlainStatusDisplay())


def plain_runtime_status_row_factory(
    status_display: Any,
    caller: str,
    *,
    kind: str,
    must_close: bool,
    config: RuntimeStatusRowConfig | None = None,
) -> AbstractAsyncContextManager[Any]:
    _cfg = config or RuntimeStatusRowConfig()
    return status_row(
        status_display,
        caller,
        kind=cast("Any", kind),
        must_close=must_close,
        config=StatusRowConfig(
            color_key=_cfg.color_key,
            work_body=_cfg.work_body,
            initial_phase=_cfg.initial_phase,
            startup_message=_cfg.startup_message,
        ),
    )
