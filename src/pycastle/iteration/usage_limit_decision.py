from datetime import datetime, timedelta

from pycastle.usage_limit_decision import (
    ContinueNow as _ContinueNow,
    PermanentlyExhausted,
    SleepUntil as _SleepUntil,
    Stop as _Stop,
    TemporaryUsageLimit,
    UsageLimitContinuationDecision,
    _sleep_message,
    decide_usage_limit_continuation as _decide_usage_limit_continuation,
)
from pycastle.services.service_registry import ServiceRegistry
from ..config import Config, StageOverride
from ..services._wake_time import compute_wake_time
from . import AbortedModelNotAvailable, AbortedUsageLimit

ContinueNow = _ContinueNow
SleepUntil = _SleepUntil
Stop = _Stop


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

    limit_outcome: TemporaryUsageLimit | PermanentlyExhausted
    if outcome.is_permanent:
        limit_outcome = PermanentlyExhausted(
            reason="credential_failure",
            provider=outcome.provider,
            raw_message=outcome.raw_message,
            account_label=outcome.account_label,
        )
    else:
        limit_outcome = TemporaryUsageLimit(
            reset_time=outcome.reset_time,
            provider=outcome.provider,
            raw_message=outcome.raw_message,
            account_label=outcome.account_label,
        )

    return _decide_usage_limit_continuation(
        limit_outcome,
        stage_override=_override_for_stage_key(cfg, outcome.stage_key),
        service_registry=service_registry,
        now=now,
        compute_wake_time=_compute_wake_time,
    )


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


def decide_model_not_available_continuation(
    outcome: AbortedModelNotAvailable,
    cfg: Config,
    service_registry: ServiceRegistry | None,
    now: datetime,
) -> UsageLimitContinuationDecision:
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

    if next_wake is not None:
        return SleepUntil(
            wake_time=next_wake,
            message=_sleep_message(next_wake, now, is_estimated=False),
        )

    service_label = outcome.service or "unknown"
    model_label = outcome.model or "unknown"
    return Stop(
        message=(
            f"Model {model_label!r} is not available on {service_label} and no other "
            "candidates have a finite wake time. Stopping."
        )
    )
