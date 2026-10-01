from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from pycastle import stage_registry
from pycastle.iteration.outcome_routing import (
    BreakLoop,
    ContinueLoop,
    SleepThenContinue,
)
from pycastle.services._wake_time import (
    _minimum_unknown_reset_duration_for_provider,
    compute_wake_time,
)

if TYPE_CHECKING:
    from datetime import datetime

    from pycastle.config import Config, StageOverride
    from pycastle.iteration import AbortedModelNotAvailable, AbortedUsageLimit
    from pycastle.services.service_registry import ServiceRegistry


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


def _permanent_exhaustion_message(outcome: AbortedUsageLimit) -> str:
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


def _registry_has_available(
    service_registry: ServiceRegistry | None,
    stage_override: StageOverride | None,
    now: datetime,
) -> bool:
    if service_registry is None:
        return False
    if stage_override is not None:
        return service_registry.has_available_for(stage_override, now)
    return service_registry.has_available(now)


def _registry_next_wake_time(
    service_registry: ServiceRegistry | None,
    stage_override: StageOverride | None,
    now: datetime,
) -> datetime | None:
    if service_registry is None:
        return None
    if stage_override is not None:
        return service_registry.next_wake_time_for(stage_override, now)
    return service_registry.next_wake_time(now)


def _compute_exhausted_wake_time(
    outcome: AbortedUsageLimit,
    service_registry: ServiceRegistry | None,
    stage_override: StageOverride | None,
    now: datetime,
) -> datetime | None:
    if service_registry is None:
        return None
    if stage_override is not None:
        exhausted_wake_time: datetime | None = None
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
        return exhausted_wake_time
    return service_registry.next_wake_time(now)


def _decide_limit_continuation(
    outcome: AbortedUsageLimit,
    *,
    cfg: Config,
    stage_override: StageOverride | None,
    service_registry: ServiceRegistry | None,
    now: datetime,
) -> ContinueLoop | SleepThenContinue | BreakLoop:
    minimum_unknown_reset_duration = _minimum_unknown_reset_duration_for_provider(
        cfg,
        outcome.provider,
    )
    if _registry_has_available(service_registry, stage_override, now):
        exhausted_wake_time = _compute_exhausted_wake_time(
            outcome, service_registry, stage_override, now
        )
        message: str | None = None
        if outcome.is_permanent:
            message = _permanent_exhaustion_message(outcome)
        elif exhausted_wake_time is not None:
            message = (
                f"Account exhausted until {_fmt_wake(exhausted_wake_time, now)}, "
                "switching to next available."
            )
        return ContinueLoop(message=message)

    next_wake = _registry_next_wake_time(service_registry, stage_override, now)
    if next_wake is not None:
        return SleepThenContinue(
            wake_time=next_wake,
            message=_sleep_message(next_wake, now, is_estimated=False),
        )

    if outcome.is_permanent:
        return BreakLoop(message=_permanent_exhaustion_message(outcome))

    wake_time, is_estimated = compute_wake_time(
        outcome.reset_time,
        now,
        minimum_unknown_reset_duration=minimum_unknown_reset_duration,
    )
    return SleepThenContinue(
        wake_time=wake_time,
        message=_sleep_message(wake_time, now, is_estimated=is_estimated),
    )


def decide_abort_continuation(
    outcome: AbortedUsageLimit | AbortedModelNotAvailable,
    cfg: Config,
    service_registry: ServiceRegistry | None,
    now: datetime,
) -> ContinueLoop | SleepThenContinue | BreakLoop:
    from pycastle.iteration import AbortedUsageLimit  # noqa: PLC0415

    stage_override = (
        stage_registry.override_for_stage_key(cfg, outcome.stage_key)
        if outcome.stage_key is not None
        else None
    )

    if isinstance(outcome, AbortedUsageLimit):
        return _decide_limit_continuation(
            outcome,
            cfg=cfg,
            stage_override=stage_override,
            service_registry=service_registry,
            now=now,
        )

    if _registry_has_available(service_registry, stage_override, now):
        return ContinueLoop()

    next_wake = _registry_next_wake_time(service_registry, stage_override, now)
    service_label = outcome.service or "unknown"
    model_label = outcome.model or "unknown"
    if next_wake is not None:
        return SleepThenContinue(
            wake_time=next_wake,
            message=(
                f"Model {model_label!r} is not available on {service_label}."
                f" Sleeping until {_fmt_wake(next_wake, now)}."
                " Press Ctrl+C to abort."
            ),
        )

    return BreakLoop(
        message=(
            f"Model {model_label!r} is not available on {service_label} and no other "
            "candidates have a finite wake time. Stopping."
        )
    )
