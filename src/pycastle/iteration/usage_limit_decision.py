from datetime import datetime, timedelta

from pycastle.usage_limit_decision import (
    ContinueNow as _ContinueNow,
    SleepUntil as _SleepUntil,
    Stop as _Stop,
    UsageLimitContinuationDecision,
    UsageLimitOutcome,
    decide_usage_limit_continuation as _decide_usage_limit_continuation,
)
from pycastle.services.service_registry import ServiceRegistry
from ..config import Config, StageOverride
from ..services._wake_time import compute_wake_time
from . import AbortedUsageLimit

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

    return _decide_usage_limit_continuation(
        UsageLimitOutcome(
            reset_time=outcome.reset_time,
            provider=outcome.provider,
            raw_message=outcome.raw_message,
            account_label=outcome.account_label,
            is_permanent=outcome.is_permanent,
        ),
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
