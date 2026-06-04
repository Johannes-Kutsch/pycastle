from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from pycastle.config import Config, StageOverride
from pycastle.iteration import AbortedUsageLimit
from pycastle.iteration.usage_limit_decision import (
    ContinueNow,
    SleepUntil,
    Stop,
    decide_usage_limit_continuation,
)
from pycastle.services.agent_service import AgentService
from pycastle.services.service_registry import ServiceRegistry


def _now() -> datetime:
    return datetime(2026, 1, 1, 14, 30, 0, tzinfo=timezone.utc)


def _make_service(*, available: bool, wake_time: datetime | None = None) -> MagicMock:
    service = MagicMock(spec=AgentService)
    service.is_available.return_value = available
    if wake_time is not None:
        service.next_wake_time.return_value = wake_time
    return service


def test_decide_usage_limit_continuation_returns_continue_now_for_stage_fallback():
    primary_wake = datetime(2026, 1, 1, 16, 0, 0, tzinfo=timezone.utc)
    registry = ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=primary_wake),
            "codex": _make_service(available=True),
            "opencode": _make_service(available=False, wake_time=_now()),
        }
    )
    cfg = Config(
        implement_override=StageOverride(
            service="claude",
            fallback=StageOverride(service="codex"),
        )
    )

    decision = decide_usage_limit_continuation(
        AbortedUsageLimit(stage_key="implement"),
        cfg,
        registry,
        _now(),
    )

    assert isinstance(decision, ContinueNow)
    assert decision.exhausted_wake_time == primary_wake


def test_decide_usage_limit_continuation_includes_same_day_switch_message():
    primary_wake = datetime(2026, 1, 1, 16, 0, 0, tzinfo=timezone.utc)
    registry = ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=primary_wake),
            "codex": _make_service(available=True),
        }
    )
    cfg = Config(
        implement_override=StageOverride(
            service="claude",
            fallback=StageOverride(service="codex"),
        )
    )

    decision = decide_usage_limit_continuation(
        AbortedUsageLimit(stage_key="implement"),
        cfg,
        registry,
        _now(),
    )

    assert decision == ContinueNow(
        message="Account exhausted until 16:00, switching to next available.",
        exhausted_wake_time=primary_wake,
    )


def test_decide_usage_limit_continuation_formats_same_local_day_switch_message():
    eastern = timezone(timedelta(hours=-5))
    now = datetime(2026, 1, 1, 20, 30, 0, tzinfo=eastern)
    primary_wake = datetime(2026, 1, 2, 1, 0, 0, tzinfo=timezone.utc)
    registry = ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=primary_wake),
            "codex": _make_service(available=True),
        }
    )
    cfg = Config(
        implement_override=StageOverride(
            service="claude",
            fallback=StageOverride(service="codex"),
        )
    )

    decision = decide_usage_limit_continuation(
        AbortedUsageLimit(stage_key="implement"),
        cfg,
        registry,
        now,
    )

    assert decision == ContinueNow(
        message="Account exhausted until 20:00, switching to next available.",
        exhausted_wake_time=primary_wake,
    )


def test_decide_usage_limit_continuation_sleeps_for_stage_chain_only():
    primary_wake = datetime(2026, 1, 1, 16, 0, 0, tzinfo=timezone.utc)
    fallback_wake = datetime(2026, 1, 1, 15, 0, 0, tzinfo=timezone.utc)
    registry = ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=primary_wake),
            "codex": _make_service(available=False, wake_time=fallback_wake),
            "opencode": _make_service(available=True),
        }
    )
    cfg = Config(
        implement_override=StageOverride(
            service="claude",
            fallback=StageOverride(service="codex"),
        )
    )

    decision = decide_usage_limit_continuation(
        AbortedUsageLimit(stage_key="implement"),
        cfg,
        registry,
        _now(),
    )

    assert decision == SleepUntil(wake_time=fallback_wake)


def test_decide_usage_limit_continuation_ignores_available_services_outside_stage_chain():
    claude_wake = datetime(2026, 1, 1, 15, 0, 0, tzinfo=timezone.utc)
    registry = ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=claude_wake),
            "codex": _make_service(available=True),
        }
    )
    cfg = Config(implement_override=StageOverride(service="missing"))

    decision = decide_usage_limit_continuation(
        AbortedUsageLimit(stage_key="implement"),
        cfg,
        registry,
        _now(),
    )

    assert decision == SleepUntil(
        wake_time=datetime(2026, 1, 1, 15, 2, 0, tzinfo=timezone.utc),
        is_estimated=True,
    )


def test_decide_usage_limit_continuation_stops_on_permanent_exhaustion():
    registry = ServiceRegistry(
        {"claude": _make_service(available=False, wake_time=_now())}
    )
    cfg = Config(implement_override=StageOverride(service="claude"))

    decision = decide_usage_limit_continuation(
        AbortedUsageLimit(stage_key="implement", is_permanent=True),
        cfg,
        registry,
        _now(),
    )

    assert decision == Stop()


def test_decide_usage_limit_continuation_estimates_wake_time_without_registry():
    now = _now()

    decision = decide_usage_limit_continuation(
        AbortedUsageLimit(reset_time=None),
        Config(),
        None,
        now,
    )

    assert decision == SleepUntil(
        wake_time=datetime(2026, 1, 1, 15, 2, 0, tzinfo=timezone.utc),
        is_estimated=True,
    )
