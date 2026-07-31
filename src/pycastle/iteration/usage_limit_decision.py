from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from pycastle.services._wake_time import compute_wake_time

if TYPE_CHECKING:
    from pycastle.config import Config, StageOverride
    from pycastle.iteration import AbortedModelNotAvailable, AbortedUsageLimit
    from pycastle.services.service_registry import ServiceRegistry


@dataclasses.dataclass(frozen=True)
class ContinueNow:
    message: str | None = None
    exhausted_wake_time: datetime | None = None


@dataclasses.dataclass(frozen=True)
class SleepUntil:
    wake_time: datetime
    message: str
    is_estimated: bool = False


@dataclasses.dataclass(frozen=True)
class Stop:
    message: str | None = None


@dataclasses.dataclass(frozen=True)
class _TemporaryUsageLimit:
    reset_time: datetime | None = None
    provider: str | None = None
    raw_message: str | None = None
    account_label: str | None = None


@dataclasses.dataclass(frozen=True)
class _PermanentlyExhausted:
    reason: str
    provider: str | None = None
    raw_message: str | None = None
    account_label: str | None = None


_WakeTimeComputer = Callable[[datetime | None, datetime], tuple[datetime, bool]]


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


def _permanent_exhaustion_message(outcome: _PermanentlyExhausted) -> str:
    provider_label = outcome.provider or "claude"
    account = outcome.account_label or "unknown"
    message = (
        f"{provider_label} {account} account retired for this run and will be retried "
        "on the next run."
    )
    if outcome.raw_message:
        message += (
            f" {_provider_message_label(provider_label)} said: {outcome.raw_message}"
        )
    return message


def _provider_message_label(provider_label: str) -> str:
    known_labels = {
        "claude": "Claude",
        "codex": "Codex",
        "opencode": "OpenCode",
    }
    return known_labels.get(provider_label, provider_label)


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


def _decide_limit_continuation(
    outcome: _TemporaryUsageLimit | _PermanentlyExhausted,
    *,
    stage_override: StageOverride | None,
    service_registry: ServiceRegistry | None,
    now: datetime,
    compute_wake_time_fn: _WakeTimeComputer,
) -> ContinueNow | SleepUntil | Stop:
    use_stage_scope = service_registry is not None and stage_override is not None

    if service_registry is None:
        has_available = False
    elif use_stage_scope:
        assert stage_override is not None
        has_available = service_registry.has_available_for(stage_override, now)
    else:
        has_available = service_registry.has_available(now)

    if has_available:
        if service_registry is None:
            exhausted_wake_time = None
        elif use_stage_scope:
            assert stage_override is not None
            exhausted_wake_time = None
            if outcome.provider is not None:
                provider_service = service_registry[outcome.provider]
                if provider_service is not None and not provider_service.is_available(
                    now=now
                ):
                    with contextlib.suppress(RuntimeError):
                        exhausted_wake_time = provider_service.next_wake_time()
            if exhausted_wake_time is None:
                exhausted_wake_time = service_registry.next_wake_time_for(
                    stage_override, now
                )
        else:
            exhausted_wake_time = service_registry.next_wake_time(now)
        message = None
        if isinstance(outcome, _PermanentlyExhausted):
            message = _permanent_exhaustion_message(outcome)
        elif exhausted_wake_time is not None:
            message = (
                f"Account exhausted until {_fmt_wake(exhausted_wake_time, now)}, "
                "switching to next available."
            )
        return ContinueNow(
            message=message,
            exhausted_wake_time=exhausted_wake_time,
        )

    if service_registry is None:
        next_wake = None
    elif use_stage_scope:
        assert stage_override is not None
        next_wake = service_registry.next_wake_time_for(stage_override, now)
    else:
        next_wake = service_registry.next_wake_time(now)

    if next_wake is not None:
        return SleepUntil(
            wake_time=next_wake,
            message=_sleep_message(next_wake, now, is_estimated=False),
        )

    if isinstance(outcome, _PermanentlyExhausted):
        return Stop(message=_permanent_exhaustion_message(outcome))

    wake_time, is_estimated = compute_wake_time_fn(outcome.reset_time, now)
    return SleepUntil(
        wake_time=wake_time,
        is_estimated=is_estimated,
        message=_sleep_message(wake_time, now, is_estimated=is_estimated),
    )


def decide_usage_limit_continuation(
    outcome: AbortedUsageLimit,
    cfg: Config,
    service_registry: ServiceRegistry | None,
    now: datetime,
) -> ContinueNow | SleepUntil | Stop:
    minimum_unknown_reset_duration = _minimum_unknown_reset_duration_for_provider(
        cfg,
        outcome.provider,
    )

    def _compute_wake_time(
        reset_time: datetime | None,
        now_: datetime,
    ) -> tuple[datetime, bool]:
        return compute_wake_time(
            reset_time,
            now_,
            minimum_unknown_reset_duration=minimum_unknown_reset_duration,
        )

    limit_outcome: _TemporaryUsageLimit | _PermanentlyExhausted
    if outcome.is_permanent:
        limit_outcome = _PermanentlyExhausted(
            reason="credential_failure",
            provider=outcome.provider,
            raw_message=outcome.raw_message,
            account_label=outcome.account_label,
        )
    else:
        limit_outcome = _TemporaryUsageLimit(
            reset_time=outcome.reset_time,
            provider=outcome.provider,
            raw_message=outcome.raw_message,
            account_label=outcome.account_label,
        )

    return _decide_limit_continuation(
        limit_outcome,
        stage_override=_override_for_stage_key(cfg, outcome.stage_key),
        service_registry=service_registry,
        now=now,
        compute_wake_time_fn=_compute_wake_time,
    )


def decide_model_not_available_continuation(
    outcome: AbortedModelNotAvailable,
    cfg: Config,
    service_registry: ServiceRegistry | None,
    now: datetime,
) -> ContinueNow | SleepUntil | Stop:
    stage_override = _override_for_stage_key(cfg, outcome.stage_key)
    use_stage_scope = service_registry is not None and stage_override is not None

    if service_registry is None:
        has_available = False
    elif use_stage_scope:
        assert stage_override is not None
        has_available = service_registry.has_available_for(stage_override, now)
    else:
        has_available = service_registry.has_available(now)

    if has_available:
        return ContinueNow()

    if service_registry is None:
        next_wake = None
    elif use_stage_scope:
        assert stage_override is not None
        next_wake = service_registry.next_wake_time_for(stage_override, now)
    else:
        next_wake = service_registry.next_wake_time(now)

    service_label = outcome.service or "unknown"
    model_label = outcome.model or "unknown"
    if next_wake is not None:
        return SleepUntil(
            wake_time=next_wake,
            message=(
                f"Model {model_label!r} is not available on {service_label}."
                f" Sleeping until {_fmt_wake(next_wake, now)}."
                " Press Ctrl+C to abort."
            ),
        )

    return Stop(
        message=(
            f"Model {model_label!r} is not available on {service_label} and no other "
            "candidates have a finite wake time. Stopping."
        )
    )
