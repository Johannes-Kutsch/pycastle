"""Tests for iteration.outcome_routing — route_outcome and LoopDirective."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock


from pycastle.config import Config
from pycastle.iteration import (
    AbortedAgentCredentialFailure,
    AbortedAgentFailure,
    AbortedHardApiError,
    AbortedHITL,
    AbortedModelNotAvailable,
    AbortedOperatorActionable,
    AbortedSetup,
    AbortedTimeout,
    AbortedUsageLimit,
    Continue,
    Done,
    MergeCloseFailure,
    NoCandidate,
)
from pycastle.iteration.outcome_routing import (
    BreakLoop,
    ContinueLoop,
    ExitFailure,
    RouterDeps,
    SleepThenContinue,
    route_outcome,
)
from pycastle.services import GithubService
from pycastle.services.runtime_services import AgentService
from pycastle.services.service_registry import ServiceRegistry
from tests.support import RecordingStatusDisplay


def _now() -> datetime:
    return datetime(2026, 1, 1, 14, 30, 0, tzinfo=timezone.utc)


def _make_deps(
    *,
    cfg: Config | None = None,
    service_registry: ServiceRegistry | None = None,
    now: datetime | None = None,
    status_display: RecordingStatusDisplay | None = None,
    github_svc: GithubService | None = None,
) -> RouterDeps:
    if cfg is None:
        cfg = Config()
    if status_display is None:
        status_display = RecordingStatusDisplay()
    if github_svc is None:
        github_svc = MagicMock(spec=GithubService)
    return RouterDeps(
        cfg=cfg,
        service_registry=service_registry,
        now=now or _now(),
        status_display=status_display,
        github_svc=github_svc,
    )


def _printed_messages(display: RecordingStatusDisplay) -> list[str]:
    return [
        str(msg) for op, *rest in display.calls if op == "print" for msg in [rest[1]]
    ]


# ── LoopDirective types ───────────────────────────────────────────────────────


def test_loop_directive_types_exist():
    assert ContinueLoop() is not None
    assert (
        SleepThenContinue(wake_time=_now(), message="sleeping", slept_once_after=True)
        is not None
    )
    assert BreakLoop() is not None
    assert ExitFailure(code=1) is not None


def test_sleep_then_continue_slept_once_after_defaults_to_true():
    d = SleepThenContinue(wake_time=_now(), message="msg")
    assert d.slept_once_after is True


# ── Continue ──────────────────────────────────────────────────────────────────


def test_route_outcome_continue_returns_continue_loop():
    display = RecordingStatusDisplay()
    result = route_outcome(Continue(), _make_deps(status_display=display))
    assert result == ContinueLoop()
    assert _printed_messages(display) == []


# ── Done ──────────────────────────────────────────────────────────────────────


def test_route_outcome_done_cap_reached_returns_break_loop_with_message():
    display = RecordingStatusDisplay()
    cfg = Config(improve_max=5)
    result = route_outcome(
        Done(improve_cap_reached=True), _make_deps(cfg=cfg, status_display=display)
    )
    assert result == BreakLoop()
    msgs = _printed_messages(display)
    assert any("improve_max" in m and "5" in m for m in msgs)


def test_route_outcome_done_no_cap_returns_break_loop_with_issue_label_message():
    display = RecordingStatusDisplay()
    cfg = Config(issue_label="my-label")
    result = route_outcome(Done(), _make_deps(cfg=cfg, status_display=display))
    assert result == BreakLoop()
    msgs = _printed_messages(display)
    assert any("my-label" in m for m in msgs)


# ── NoCandidate ───────────────────────────────────────────────────────────────


def test_route_outcome_no_candidate_returns_break_loop_with_message():
    display = RecordingStatusDisplay()
    result = route_outcome(NoCandidate(), _make_deps(status_display=display))
    assert result == BreakLoop()
    msgs = _printed_messages(display)
    assert any("no improvement candidate" in m.lower() for m in msgs)


# ── AbortedHITL ───────────────────────────────────────────────────────────────


def test_route_outcome_aborted_hitl_returns_exit_failure():
    result = route_outcome(AbortedHITL(issue_number=7), _make_deps())
    assert result == ExitFailure(code=1)


# ── AbortedAgentCredentialFailure ─────────────────────────────────────────────


def test_route_outcome_aborted_agent_credential_failure_returns_exit_failure():
    result = route_outcome(AbortedAgentCredentialFailure(status_code=401), _make_deps())
    assert result == ExitFailure(code=1)


# ── AbortedHardApiError ───────────────────────────────────────────────────────


def test_route_outcome_aborted_hard_api_error_returns_exit_failure():
    result = route_outcome(AbortedHardApiError(status_code=500), _make_deps())
    assert result == ExitFailure(code=1)


# ── AbortedAgentFailure ───────────────────────────────────────────────────────


def test_route_outcome_aborted_agent_failure_returns_exit_failure_with_message():
    display = RecordingStatusDisplay()
    result = route_outcome(
        AbortedAgentFailure(failed_role="Implementer"),
        _make_deps(status_display=display),
    )
    assert result == ExitFailure(code=1)
    msgs = _printed_messages(display)
    assert any("Implementer" in m for m in msgs)


def test_route_outcome_aborted_agent_failure_with_issue_number_includes_issue_in_message():
    display = RecordingStatusDisplay()
    result = route_outcome(
        AbortedAgentFailure(failed_role="Planner", issue_number=42),
        _make_deps(status_display=display),
    )
    assert result == ExitFailure(code=1)
    msgs = _printed_messages(display)
    assert any("#42" in m for m in msgs)


# ── AbortedTimeout ────────────────────────────────────────────────────────────


def test_route_outcome_aborted_timeout_returns_continue_loop_with_message():
    display = RecordingStatusDisplay()
    result = route_outcome(
        AbortedTimeout(failed_role="Merger", worktree_path=Path("/tmp/wt")),
        _make_deps(status_display=display),
    )
    assert result == ContinueLoop()
    msgs = _printed_messages(display)
    assert any("Merger" in m and "timed out" in m for m in msgs)


# ── AbortedOperatorActionable ─────────────────────────────────────────────────


def test_route_outcome_aborted_operator_actionable_returns_exit_failure_and_files_issue():
    display = RecordingStatusDisplay()
    github_svc = MagicMock(spec=GithubService)
    github_svc.repo = "owner/repo"
    github_svc.search_open_issues_by_title.return_value = []
    github_svc.create_issue_in.return_value = 99

    result = route_outcome(
        AbortedOperatorActionable(
            op="push", stderr="connection refused", attempt_count=3
        ),
        _make_deps(status_display=display, github_svc=github_svc),
    )
    assert result == ExitFailure(code=1)
    msgs = _printed_messages(display)
    assert any("push" in m and "3" in m for m in msgs)
    github_svc.search_open_issues_by_title.assert_called_once()


# ── MergeCloseFailure ─────────────────────────────────────────────────────────


def test_route_outcome_merge_close_failure_returns_break_loop_with_filed_numbers():
    display = RecordingStatusDisplay()
    result = route_outcome(
        MergeCloseFailure(filed_issue_numbers=[10, 20]),
        _make_deps(status_display=display),
    )
    assert result == BreakLoop()
    msgs = _printed_messages(display)
    assert any("#10" in m and "#20" in m for m in msgs)


# ── AbortedSetup ──────────────────────────────────────────────────────────────


def test_route_outcome_aborted_setup_returns_exit_failure_and_prints_phase_message():
    display = RecordingStatusDisplay()
    result = route_outcome(
        AbortedSetup(phase="lint", message="ruff failed", command=None, output=None),
        _make_deps(status_display=display),
    )
    assert result == ExitFailure(code=1)
    msgs = _printed_messages(display)
    assert any("lint" in m and "ruff failed" in m for m in msgs)


def test_route_outcome_aborted_setup_with_command_and_output_includes_them_in_message():
    display = RecordingStatusDisplay()
    result = route_outcome(
        AbortedSetup(
            phase="test",
            message="pytest failed",
            command="pytest tests/",
            output="FAILED tests/foo.py",
        ),
        _make_deps(status_display=display),
    )
    assert result == ExitFailure(code=1)
    msgs = _printed_messages(display)
    combined = " ".join(msgs)
    assert "pytest tests/" in combined
    assert "FAILED tests/foo.py" in combined


def test_route_outcome_aborted_setup_without_command_omits_command_from_message():
    display = RecordingStatusDisplay()
    route_outcome(
        AbortedSetup(phase="test", message="failed", command=None, output=None),
        _make_deps(status_display=display),
    )
    msgs = _printed_messages(display)
    combined = " ".join(msgs)
    assert "Command:" not in combined
    assert "Output:" not in combined


# ── AbortedUsageLimit ─────────────────────────────────────────────────────────


def test_route_outcome_aborted_usage_limit_permanent_no_registry_returns_break_loop():
    result = route_outcome(
        AbortedUsageLimit(is_permanent=True),
        _make_deps(service_registry=None),
    )
    assert result == BreakLoop()


def test_route_outcome_aborted_usage_limit_temporary_no_registry_returns_sleep_then_continue():
    reset = _now() + timedelta(hours=2)
    result = route_outcome(
        AbortedUsageLimit(is_permanent=False, reset_time=reset, provider="claude"),
        _make_deps(service_registry=None),
    )
    assert isinstance(result, SleepThenContinue)
    assert result.wake_time > _now()
    assert result.slept_once_after is True
    assert "Sleeping until" in result.message


def test_route_outcome_aborted_usage_limit_with_fallback_service_returns_continue_loop():
    available_svc = MagicMock(spec=AgentService)
    available_svc.is_available.return_value = True
    registry = ServiceRegistry({"codex": available_svc})
    cfg = Config()

    result = route_outcome(
        AbortedUsageLimit(is_permanent=False, provider="claude", stage_key="plan"),
        _make_deps(cfg=cfg, service_registry=registry),
    )
    assert result == ContinueLoop()


# ── AbortedModelNotAvailable ──────────────────────────────────────────────────


def test_route_outcome_aborted_model_not_available_no_registry_returns_break_loop():
    display = RecordingStatusDisplay()
    result = route_outcome(
        AbortedModelNotAvailable(service="codex", model="gpt-5.3"),
        _make_deps(service_registry=None, status_display=display),
    )
    assert result == BreakLoop()
    msgs = _printed_messages(display)
    assert any("gpt-5.3" in m for m in msgs)


def test_route_outcome_aborted_model_not_available_with_wake_time_returns_sleep_then_continue():
    wake = _now() + timedelta(hours=1)
    unavailable_svc = MagicMock(spec=AgentService)
    unavailable_svc.is_available.return_value = False
    unavailable_svc.next_wake_time.return_value = wake
    registry = ServiceRegistry({"codex": unavailable_svc})

    result = route_outcome(
        AbortedModelNotAvailable(service="codex", model="gpt-5.3"),
        _make_deps(service_registry=registry),
    )
    assert isinstance(result, SleepThenContinue)
    assert result.wake_time == wake
    assert result.slept_once_after is True


def test_route_outcome_aborted_model_not_available_with_available_service_returns_continue_loop():
    available_svc = MagicMock(spec=AgentService)
    available_svc.is_available.return_value = True
    registry = ServiceRegistry({"claude": available_svc})
    cfg = Config()

    result = route_outcome(
        AbortedModelNotAvailable(service="codex", model="gpt-5.3", stage_key="plan"),
        _make_deps(cfg=cfg, service_registry=registry),
    )
    assert result == ContinueLoop()
