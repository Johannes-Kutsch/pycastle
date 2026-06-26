from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from pycastle.config import Config
from pycastle.iteration import AbortedUsageLimit
from pycastle.iteration.usage_limit_decision import (
    decide_usage_limit_continuation as decide_iteration_usage_limit_continuation,
)
from pycastle.config.types import StageOverride
from pycastle.services.service_registry import ServiceRegistry
from pycastle.usage_limit_decision import (
    ContinueNow,
    PermanentlyExhausted,
    SleepUntil,
    Stop,
    TemporaryUsageLimit,
    UsageLimitContinuationDecision,
    decide_usage_limit_continuation,
)
from pycastle.services._wake_time import compute_wake_time
from pycastle.services.runtime_services import AgentService

runtime: Any = SimpleNamespace(
    ContinueNow=ContinueNow,
    PermanentlyExhausted=PermanentlyExhausted,
    ServiceRegistry=ServiceRegistry,
    SleepUntil=SleepUntil,
    StageOverride=StageOverride,
    Stop=Stop,
    TemporaryUsageLimit=TemporaryUsageLimit,
    UsageLimitContinuationDecision=UsageLimitContinuationDecision,
    decide_usage_limit_continuation=decide_usage_limit_continuation,
)


def _now() -> datetime:
    return datetime(2026, 1, 1, 14, 30, 0, tzinfo=timezone.utc)


def test_usage_limit_module_exports_distinct_outcome_types():
    import pycastle.usage_limit_decision as usage_limit_module

    assert usage_limit_module.TemporaryUsageLimit is TemporaryUsageLimit
    assert usage_limit_module.PermanentlyExhausted is PermanentlyExhausted
    assert not hasattr(usage_limit_module, "UsageLimitOutcome")


def _make_service(*, available: bool, wake_time: datetime | None = None) -> MagicMock:
    service = MagicMock(spec=AgentService)
    service.is_available.return_value = available
    if wake_time is not None:
        service.next_wake_time.return_value = wake_time
    return service


def _stage_override(
    service: str, fallback_service: str | None = None
) -> runtime.StageOverride:
    return runtime.StageOverride(
        service=service,
        fallback=(
            None
            if fallback_service is None
            else runtime.StageOverride(service=fallback_service)
        ),
    )


def _decide(
    outcome: runtime.TemporaryUsageLimit | runtime.PermanentlyExhausted,
    *,
    stage_override: runtime.StageOverride | None,
    service_registry: runtime.ServiceRegistry | None,
    now: datetime,
) -> runtime.UsageLimitContinuationDecision:
    return runtime.decide_usage_limit_continuation(
        outcome,
        stage_override=stage_override,
        service_registry=service_registry,
        now=now,
        compute_wake_time=compute_wake_time,
    )


def test_decide_usage_limit_continuation_returns_continue_now_for_stage_fallback():
    primary_wake = datetime(2026, 1, 1, 16, 0, 0, tzinfo=timezone.utc)
    registry = runtime.ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=primary_wake),
            "codex": _make_service(available=True),
            "opencode": _make_service(available=False, wake_time=_now()),
        }
    )

    decision = _decide(
        runtime.TemporaryUsageLimit(),
        stage_override=_stage_override("claude", "codex"),
        service_registry=registry,
        now=_now(),
    )

    assert isinstance(decision, runtime.ContinueNow)
    assert decision.exhausted_wake_time == primary_wake


def test_decide_usage_limit_continuation_includes_same_day_switch_message():
    primary_wake = datetime(2026, 1, 1, 16, 0, 0, tzinfo=timezone.utc)
    registry = runtime.ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=primary_wake),
            "codex": _make_service(available=True),
        }
    )

    decision = _decide(
        runtime.TemporaryUsageLimit(),
        stage_override=_stage_override("claude", "codex"),
        service_registry=registry,
        now=_now(),
    )

    assert decision == runtime.ContinueNow(
        message="Account exhausted until 16:00, switching to next available.",
        exhausted_wake_time=primary_wake,
    )


def test_decide_usage_limit_continuation_formats_same_local_day_switch_message():
    eastern = timezone(timedelta(hours=-5))
    now = datetime(2026, 1, 1, 20, 30, 0, tzinfo=eastern)
    primary_wake = datetime(2026, 1, 2, 1, 0, 0, tzinfo=timezone.utc)
    registry = runtime.ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=primary_wake),
            "codex": _make_service(available=True),
        }
    )

    decision = _decide(
        runtime.TemporaryUsageLimit(),
        stage_override=_stage_override("claude", "codex"),
        service_registry=registry,
        now=now,
    )

    assert decision == runtime.ContinueNow(
        message="Account exhausted until 20:00, switching to next available.",
        exhausted_wake_time=primary_wake,
    )


def test_decide_usage_limit_continuation_sleeps_for_stage_chain_only():
    primary_wake = datetime(2026, 1, 1, 16, 0, 0, tzinfo=timezone.utc)
    fallback_wake = datetime(2026, 1, 1, 15, 0, 0, tzinfo=timezone.utc)
    registry = runtime.ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=primary_wake),
            "codex": _make_service(available=False, wake_time=fallback_wake),
            "opencode": _make_service(available=True),
        }
    )

    decision = _decide(
        runtime.TemporaryUsageLimit(),
        stage_override=_stage_override("claude", "codex"),
        service_registry=registry,
        now=_now(),
    )

    assert isinstance(decision, runtime.SleepUntil)
    assert decision.wake_time == fallback_wake
    assert (
        decision.message
        == "Usage limit reached. Sleeping until 15:00. Press Ctrl+C to abort."
    )


def test_decide_usage_limit_continuation_keeps_failing_service_wake_on_continue_when_other_stage_services_are_exhausted():
    failing_wake = datetime(2026, 1, 1, 16, 0, 0, tzinfo=timezone.utc)
    fallback_wake = datetime(2026, 1, 1, 15, 0, 0, tzinfo=timezone.utc)
    registry = runtime.ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=failing_wake),
            "codex": _make_service(available=False, wake_time=fallback_wake),
            "opencode": _make_service(available=True),
        }
    )

    decision = _decide(
        runtime.TemporaryUsageLimit(provider="claude"),
        stage_override=runtime.StageOverride(
            service="claude",
            fallback=runtime.StageOverride(
                service="codex",
                fallback=runtime.StageOverride(service="opencode"),
            ),
        ),
        service_registry=registry,
        now=_now(),
    )

    assert decision == runtime.ContinueNow(
        message="Account exhausted until 16:00, switching to next available.",
        exhausted_wake_time=failing_wake,
    )


def test_decide_usage_limit_continuation_formats_cross_day_sleep_message():
    now = datetime(2026, 1, 1, 23, 30, 0, tzinfo=timezone.utc)
    fallback_wake = datetime(2026, 1, 2, 1, 0, 0, tzinfo=timezone.utc)
    registry = runtime.ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=fallback_wake),
            "codex": _make_service(available=False, wake_time=fallback_wake),
            "opencode": _make_service(available=True),
        }
    )

    decision = _decide(
        runtime.TemporaryUsageLimit(),
        stage_override=_stage_override("claude", "codex"),
        service_registry=registry,
        now=now,
    )

    assert isinstance(decision, runtime.SleepUntil)
    assert (
        decision.message
        == "Usage limit reached. Sleeping until Jan 2, 01:00. Press Ctrl+C to abort."
    )


def test_decide_usage_limit_continuation_formats_same_local_day_sleep_message():
    eastern = timezone(timedelta(hours=-5))
    now = datetime(2026, 1, 1, 20, 30, 0, tzinfo=eastern)
    fallback_wake = datetime(2026, 1, 2, 1, 0, 0, tzinfo=timezone.utc)
    registry = runtime.ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=fallback_wake),
            "codex": _make_service(available=False, wake_time=fallback_wake),
        }
    )

    decision = _decide(
        runtime.TemporaryUsageLimit(),
        stage_override=_stage_override("claude", "codex"),
        service_registry=registry,
        now=now,
    )

    assert isinstance(decision, runtime.SleepUntil)
    assert (
        decision.message
        == "Usage limit reached. Sleeping until 20:00. Press Ctrl+C to abort."
    )


def test_decide_usage_limit_continuation_ignores_exhausted_services_outside_stage_chain():
    stage_wake = datetime(2026, 1, 1, 15, 0, 0, tzinfo=timezone.utc)
    unrelated_wake = datetime(2026, 1, 1, 14, 45, 0, tzinfo=timezone.utc)
    registry = runtime.ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=stage_wake),
            "codex": _make_service(available=False, wake_time=stage_wake),
            "opencode": _make_service(available=False, wake_time=unrelated_wake),
        }
    )

    decision = _decide(
        runtime.TemporaryUsageLimit(),
        stage_override=_stage_override("claude", "codex"),
        service_registry=registry,
        now=_now(),
    )

    assert isinstance(decision, runtime.SleepUntil)
    assert decision.wake_time == stage_wake
    assert (
        decision.message
        == "Usage limit reached. Sleeping until 15:00. Press Ctrl+C to abort."
    )


def test_decide_usage_limit_continuation_ignores_available_services_outside_stage_chain():
    claude_wake = datetime(2026, 1, 1, 15, 0, 0, tzinfo=timezone.utc)
    registry = runtime.ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=claude_wake),
            "codex": _make_service(available=True),
        }
    )

    decision = _decide(
        runtime.TemporaryUsageLimit(),
        stage_override=runtime.StageOverride(service="missing"),
        service_registry=registry,
        now=_now(),
    )

    assert isinstance(decision, runtime.SleepUntil)
    assert decision.wake_time == datetime(2026, 1, 1, 15, 2, 0, tzinfo=timezone.utc)
    assert decision.is_estimated is True
    assert (
        decision.message == "Usage limit reached. Sleeping until 15:02 (estimated)."
        " Press Ctrl+C to abort."
    )


def test_decide_usage_limit_continuation_uses_global_fallback_when_stage_priority_chain_is_missing():
    primary_wake = datetime(2026, 1, 1, 16, 0, 0, tzinfo=timezone.utc)
    registry = runtime.ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=primary_wake),
            "codex": _make_service(available=True),
        }
    )

    decision = _decide(
        runtime.TemporaryUsageLimit(),
        stage_override=None,
        service_registry=registry,
        now=_now(),
    )

    assert decision == runtime.ContinueNow(
        message="Account exhausted until 16:00, switching to next available.",
        exhausted_wake_time=primary_wake,
    )


def test_decide_usage_limit_continuation_uses_global_next_wake_when_stage_priority_chain_is_missing():
    primary_wake = datetime(2026, 1, 1, 16, 0, 0, tzinfo=timezone.utc)
    fallback_wake = datetime(2026, 1, 1, 15, 0, 0, tzinfo=timezone.utc)
    registry = runtime.ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=primary_wake),
            "codex": _make_service(available=False, wake_time=fallback_wake),
        }
    )

    decision = _decide(
        runtime.TemporaryUsageLimit(),
        stage_override=None,
        service_registry=registry,
        now=_now(),
    )

    assert decision == runtime.SleepUntil(
        wake_time=fallback_wake,
        message="Usage limit reached. Sleeping until 15:00. Press Ctrl+C to abort.",
        is_estimated=False,
    )


def test_decide_usage_limit_continuation_stops_on_permanent_exhaustion():
    registry = runtime.ServiceRegistry(
        {"claude": _make_service(available=False, wake_time=_now())}
    )

    decision = _decide(
        runtime.PermanentlyExhausted(reason="credential_failure"),
        stage_override=runtime.StageOverride(service="claude"),
        service_registry=registry,
        now=_now(),
    )

    assert decision == runtime.Stop(
        message=(
            "claude unknown account retired for this run and will be retried on the "
            "next run."
        )
    )


def test_decide_usage_limit_continuation_returns_continue_now_for_permanent_exhaustion_with_fallback():
    denial = "disabled Claude subscription access for Claude Code"
    primary_wake = datetime(2026, 1, 1, 16, 0, 0, tzinfo=timezone.utc)
    registry = runtime.ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=primary_wake),
            "codex": _make_service(available=True),
        }
    )

    decision = _decide(
        runtime.PermanentlyExhausted(
            reason="credential_failure",
            provider="claude",
            account_label="secondary",
            raw_message=denial,
        ),
        stage_override=_stage_override("claude", "codex"),
        service_registry=registry,
        now=_now(),
    )

    assert decision == runtime.ContinueNow(
        message=(
            "claude secondary account retired for this run and will be retried on "
            "the next run. Claude said: disabled Claude subscription access for "
            "Claude Code"
        ),
        exhausted_wake_time=primary_wake,
    )


def test_decide_usage_limit_continuation_stops_on_permanent_exhaustion_without_configured_fallback():
    denial = "disabled Claude subscription access for Claude Code"
    registry = runtime.ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=_now()),
            "codex": _make_service(available=True),
        }
    )

    decision = _decide(
        runtime.PermanentlyExhausted(
            reason="credential_failure",
            provider="claude",
            account_label="primary",
            raw_message=denial,
        ),
        stage_override=runtime.StageOverride(service="claude"),
        service_registry=registry,
        now=_now(),
    )

    assert decision == runtime.Stop(
        message=(
            "claude primary account retired for this run and will be retried on "
            "the next run. Claude said: disabled Claude subscription access for "
            "Claude Code"
        )
    )


def test_decide_usage_limit_continuation_uses_observed_provider_label_for_non_claude_permanent_message():
    registry = runtime.ServiceRegistry(
        {"opencode": _make_service(available=False, wake_time=_now())}
    )

    decision = _decide(
        runtime.PermanentlyExhausted(
            reason="credential_failure",
            provider="OpenCode",
            account_label="primary",
            raw_message="usage limit reached for this account",
        ),
        stage_override=runtime.StageOverride(service="opencode"),
        service_registry=registry,
        now=_now(),
    )

    assert decision == runtime.Stop(
        message=(
            "OpenCode primary account retired for this run and will be retried on "
            "the next run. OpenCode said: usage limit reached for this account"
        )
    )


def test_decide_usage_limit_continuation_estimates_wake_time_without_registry():
    now = _now()

    decision = _decide(
        runtime.TemporaryUsageLimit(reset_time=None),
        stage_override=None,
        service_registry=None,
        now=now,
    )

    assert isinstance(decision, runtime.SleepUntil)
    assert decision.wake_time == datetime(2026, 1, 1, 15, 2, 0, tzinfo=timezone.utc)
    assert decision.is_estimated is True
    assert (
        decision.message == "Usage limit reached. Sleeping until 15:02 (estimated)."
        " Press Ctrl+C to abort."
    )


def test_decide_usage_limit_continuation_uses_exact_reset_time_without_registry():
    now = _now()
    reset_time = datetime(2026, 1, 1, 15, 30, 0, tzinfo=timezone.utc)

    decision = _decide(
        runtime.TemporaryUsageLimit(reset_time=reset_time),
        stage_override=None,
        service_registry=None,
        now=now,
    )

    assert isinstance(decision, runtime.SleepUntil)
    assert decision.wake_time == datetime(2026, 1, 1, 15, 32, 0, tzinfo=timezone.utc)
    assert decision.is_estimated is False
    assert (
        decision.message
        == "Usage limit reached. Sleeping until 15:32. Press Ctrl+C to abort."
    )


def test_decide_usage_limit_continuation_formats_cross_day_exact_reset_without_registry():
    now = datetime(2026, 1, 1, 23, 30, 0, tzinfo=timezone.utc)
    reset_time = datetime(2026, 1, 2, 0, 30, 0, tzinfo=timezone.utc)

    decision = _decide(
        runtime.TemporaryUsageLimit(reset_time=reset_time),
        stage_override=None,
        service_registry=None,
        now=now,
    )

    assert isinstance(decision, runtime.SleepUntil)
    assert (
        decision.message
        == "Usage limit reached. Sleeping until Jan 2, 00:32. Press Ctrl+C to abort."
    )


def test_decide_usage_limit_continuation_keeps_stage_key_behavior_without_registry():
    now = _now()

    decision = _decide(
        runtime.TemporaryUsageLimit(reset_time=None),
        stage_override=runtime.StageOverride(service="claude"),
        service_registry=None,
        now=now,
    )

    assert isinstance(decision, runtime.SleepUntil)
    assert decision.wake_time == datetime(2026, 1, 1, 15, 2, 0, tzinfo=timezone.utc)
    assert decision.is_estimated is True
    assert (
        decision.message == "Usage limit reached. Sleeping until 15:02 (estimated)."
        " Press Ctrl+C to abort."
    )


def test_iteration_usage_limit_continuation_uses_provider_minimum_duration_for_unknown_reset():
    decision = decide_iteration_usage_limit_continuation(
        AbortedUsageLimit(
            provider="codex",
            reset_time=None,
            stage_key="review",
        ),
        Config(
            codex_minimum_unknown_reset_duration_hours=1.5,
        ),
        service_registry=None,
        now=_now(),
    )

    assert isinstance(decision, SleepUntil)
    assert decision.wake_time == datetime(2026, 1, 1, 16, 2, 0, tzinfo=timezone.utc)
    assert decision.is_estimated is True
    assert (
        decision.message == "Usage limit reached. Sleeping until 16:02 (estimated)."
        " Press Ctrl+C to abort."
    )


def test_iteration_usage_limit_continuation_keeps_parsed_reset_time_authoritative():
    reset_time = datetime(2026, 1, 1, 15, 30, 0, tzinfo=timezone.utc)

    decision = decide_iteration_usage_limit_continuation(
        AbortedUsageLimit(
            provider="codex",
            reset_time=reset_time,
            stage_key="review",
        ),
        Config(
            codex_minimum_unknown_reset_duration_hours=6,
        ),
        service_registry=None,
        now=_now(),
    )

    assert isinstance(decision, SleepUntil)
    assert decision.wake_time == datetime(2026, 1, 1, 15, 32, 0, tzinfo=timezone.utc)
    assert decision.is_estimated is False
    assert (
        decision.message
        == "Usage limit reached. Sleeping until 15:32. Press Ctrl+C to abort."
    )
