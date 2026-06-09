from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pycastle_agent_runtime as runtime

from pycastle.services._wake_time import compute_wake_time
from pycastle.services.agent_service import AgentService


def _now() -> datetime:
    return datetime(2026, 1, 1, 14, 30, 0, tzinfo=timezone.utc)


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
    outcome: runtime.UsageLimitOutcome,
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
        runtime.UsageLimitOutcome(),
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
        runtime.UsageLimitOutcome(),
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
        runtime.UsageLimitOutcome(),
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
        runtime.UsageLimitOutcome(),
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
        runtime.UsageLimitOutcome(),
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
        runtime.UsageLimitOutcome(),
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
        runtime.UsageLimitOutcome(),
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
        runtime.UsageLimitOutcome(),
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


def test_decide_usage_limit_continuation_stops_on_permanent_exhaustion():
    registry = runtime.ServiceRegistry(
        {"claude": _make_service(available=False, wake_time=_now())}
    )

    decision = _decide(
        runtime.UsageLimitOutcome(is_permanent=True),
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
        runtime.UsageLimitOutcome(
            provider="claude",
            account_label="secondary",
            raw_message=denial,
            is_permanent=True,
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
        runtime.UsageLimitOutcome(
            provider="claude",
            account_label="primary",
            raw_message=denial,
            is_permanent=True,
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


def test_decide_usage_limit_continuation_estimates_wake_time_without_registry():
    now = _now()

    decision = _decide(
        runtime.UsageLimitOutcome(reset_time=None),
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
        runtime.UsageLimitOutcome(reset_time=reset_time),
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
        runtime.UsageLimitOutcome(reset_time=reset_time),
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
        runtime.UsageLimitOutcome(reset_time=None),
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
