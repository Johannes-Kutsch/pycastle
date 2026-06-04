from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import TypeAlias

from ..config import Config, StageOverride
from ..services import ServiceRegistry
from ..services._wake_time import compute_wake_time
from . import AbortedUsageLimit


@dataclasses.dataclass(frozen=True)
class ContinueNow:
    message: str | None = None
    exhausted_wake_time: datetime | None = None


@dataclasses.dataclass(frozen=True)
class SleepUntil:
    wake_time: datetime
    is_estimated: bool = False
    message: str | None = None


@dataclasses.dataclass(frozen=True)
class Stop:
    pass


UsageLimitContinuationDecision: TypeAlias = ContinueNow | SleepUntil | Stop


def _fmt_wake(wake: datetime, now: datetime) -> str:
    local_wake = wake.astimezone(now.tzinfo) if now.tzinfo is not None else wake
    if local_wake.date() != now.date():
        return f"{local_wake:%b} {local_wake.day}, {local_wake:%H:%M}"
    return local_wake.strftime("%H:%M")


def _sleep_message(wake: datetime, now: datetime, *, is_estimated: bool) -> str:
    suffix = " (estimated)" if is_estimated else ""
    return (
        f"Usage limit reached. Sleeping until {_fmt_wake(wake, now)}{suffix}."
        " Press Ctrl+C to abort."
    )


def _override_for_stage_key(cfg: Config, stage_key: str | None) -> StageOverride | None:
    if stage_key == "plan":
        return cfg.plan_override
    if stage_key == "implement":
        return cfg.implement_override
    if stage_key == "review":
        return cfg.review_override
    if stage_key == "merge":
        return cfg.merge_override
    if stage_key == "preflight_issue":
        return cfg.preflight_issue_override
    if stage_key == "improve":
        return cfg.improve_override
    return None


def decide_usage_limit_continuation(
    outcome: AbortedUsageLimit,
    cfg: Config,
    service_registry: ServiceRegistry | None,
    now: datetime,
) -> UsageLimitContinuationDecision:
    stage_override = _override_for_stage_key(cfg, outcome.stage_key)
    scoped_override = stage_override
    use_stage_scope = service_registry is not None and scoped_override is not None

    if service_registry is None:
        has_available = False
    elif use_stage_scope:
        assert scoped_override is not None
        has_available = service_registry.has_available_for(scoped_override, now)
    else:
        has_available = service_registry.has_available(now)

    if has_available:
        if service_registry is None:
            exhausted_wake_time = None
        elif use_stage_scope:
            assert scoped_override is not None
            exhausted_wake_time = service_registry.next_wake_time_for(
                scoped_override, now
            )
        else:
            exhausted_wake_time = service_registry.next_wake_time(now)
        message = None
        if not outcome.is_permanent and exhausted_wake_time is not None:
            message = (
                f"Account exhausted until {_fmt_wake(exhausted_wake_time, now)}, "
                "switching to next available."
            )
        return ContinueNow(
            message=message,
            exhausted_wake_time=exhausted_wake_time,
        )

    if outcome.is_permanent:
        return Stop()

    if service_registry is None:
        next_wake = None
    elif use_stage_scope:
        assert scoped_override is not None
        next_wake = service_registry.next_wake_time_for(scoped_override, now)
    else:
        next_wake = service_registry.next_wake_time(now)

    if next_wake is not None:
        return SleepUntil(
            wake_time=next_wake,
            message=_sleep_message(next_wake, now, is_estimated=False),
        )

    wake_time, is_estimated = compute_wake_time(outcome.reset_time, now)
    return SleepUntil(
        wake_time=wake_time,
        is_estimated=is_estimated,
        message=_sleep_message(wake_time, now, is_estimated=is_estimated),
    )
