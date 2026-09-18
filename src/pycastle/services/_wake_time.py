from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pycastle.config import Config

_BUFFER = timedelta(minutes=2)


def _minimum_unknown_reset_duration_for_provider(
    cfg: Config,
    provider: str | None,
) -> timedelta:
    if provider == "claude":
        return timedelta(hours=cfg.claude_minimum_unknown_reset_duration_hours)
    if provider == "codex":
        return timedelta(hours=cfg.codex_minimum_unknown_reset_duration_hours)
    if provider == "opencode":
        return timedelta(hours=cfg.opencode_minimum_unknown_reset_duration_hours)
    return timedelta(0)


def compute_wake_time(
    reset_time: datetime | None,
    now: datetime,
    minimum_unknown_reset_duration: timedelta = timedelta(0),
) -> tuple[datetime, bool]:
    if reset_time is not None:
        return reset_time + _BUFFER, False

    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    wake = next_hour + _BUFFER
    if minimum_unknown_reset_duration <= timedelta(0):
        return wake, True

    minimum_reset_time = now + minimum_unknown_reset_duration
    while wake < minimum_reset_time:
        wake += timedelta(hours=1)
    return wake, True
