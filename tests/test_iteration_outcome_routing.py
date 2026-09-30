"""Tests for iteration.outcome_routing — route_outcome and LoopDirective."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from pycastle.config import Config
from pycastle.config.types import StageOverride
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
    return datetime(2026, 1, 1, 14, 30, 0, tzinfo=UTC)


class RecordingBugFiler:
    """In-memory bug filer that captures calls and returns a controllable URL."""

    def __init__(
        self, return_url: str | None = "https://github.com/owner/repo/issues/1"
    ) -> None:
        self.calls: list[tuple[str, str, list[str]]] = []
        self._url = return_url

    def __call__(
        self,
        title: str,
        body: str,
        labels: list[str],
        *,
        cfg: Config | None = None,
    ) -> str | None:
        self.calls.append((title, body, labels))
        return self._url


def _make_deps(
    *,
    cfg: Config | None = None,
    service_registry: ServiceRegistry | None = None,
    now: datetime | None = None,
    status_display: RecordingStatusDisplay | None = None,
    github_svc: GithubService | None = None,
    bug_filer: RecordingBugFiler | None = None,
) -> RouterDeps:
    if cfg is None:
        cfg = Config()
    if status_display is None:
        status_display = RecordingStatusDisplay()
    if github_svc is None:
        github_svc = MagicMock(spec=GithubService)
    if bug_filer is None:
        bug_filer = RecordingBugFiler(return_url=None)
    return RouterDeps(
        cfg=cfg,
        service_registry=service_registry,
        now=now or _now(),
        status_display=status_display,
        github_svc=github_svc,
        bug_filer=bug_filer,
    )


def _printed_messages(display: RecordingStatusDisplay) -> list[str]:
    return [
        str(msg) for op, *rest in display.calls if op == "print" for msg in [rest[1]]
    ]


# ── LoopDirective types ───────────────────────────────────────────────────────


def test_loop_directive_types_exist():
    assert ContinueLoop() is not None
    assert SleepThenContinue(wake_time=_now(), message="sleeping") is not None
    assert BreakLoop() is not None
    assert ExitFailure(code=1) is not None


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
    assert isinstance(result, BreakLoop)
    assert result.message is not None
    assert "improve_max" in result.message
    assert "5" in result.message
    assert _printed_messages(display) == []


def test_route_outcome_done_no_cap_returns_break_loop_with_issue_label_message():
    display = RecordingStatusDisplay()
    cfg = Config(issue_label="my-label")
    result = route_outcome(Done(), _make_deps(cfg=cfg, status_display=display))
    assert isinstance(result, BreakLoop)
    assert result.message is not None
    assert "my-label" in result.message
    assert _printed_messages(display) == []


# ── NoCandidate ───────────────────────────────────────────────────────────────


def test_route_outcome_no_candidate_returns_break_loop_with_message():
    display = RecordingStatusDisplay()
    result = route_outcome(NoCandidate(), _make_deps(status_display=display))
    assert isinstance(result, BreakLoop)
    assert result.message is not None
    assert "no improvement candidate" in result.message.lower()
    assert _printed_messages(display) == []


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
    assert isinstance(result, ExitFailure)
    assert result.code == 1
    assert result.message is not None
    assert "Implementer" in result.message
    assert _printed_messages(display) == []


def test_route_outcome_aborted_agent_failure_with_issue_number_includes_issue_in_message():
    display = RecordingStatusDisplay()
    result = route_outcome(
        AbortedAgentFailure(failed_role="Planner", issue_number=42),
        _make_deps(status_display=display),
    )
    assert isinstance(result, ExitFailure)
    assert result.code == 1
    assert result.message is not None
    assert "#42" in result.message
    assert _printed_messages(display) == []


# ── AbortedTimeout ────────────────────────────────────────────────────────────


def test_route_outcome_aborted_timeout_returns_continue_loop_with_message():
    display = RecordingStatusDisplay()
    result = route_outcome(
        AbortedTimeout(failed_role="Merger", worktree_path=Path("/tmp/wt")),
        _make_deps(status_display=display),
    )
    assert isinstance(result, ContinueLoop)
    assert result.message is not None
    assert "Merger" in result.message
    assert "timed out" in result.message
    assert _printed_messages(display) == []


# ── AbortedOperatorActionable ─────────────────────────────────────────────────


def test_route_outcome_aborted_operator_actionable_returns_exit_failure_and_files_issue():
    display = RecordingStatusDisplay()
    github_svc = MagicMock(spec=GithubService)
    github_svc.repo = "owner/repo"
    github_svc.search_open_issues_by_title.return_value = []
    github_svc.create_issue_in.return_value = (99, 10099)

    result = route_outcome(
        AbortedOperatorActionable(
            op="push", stderr="connection refused", attempt_count=3
        ),
        _make_deps(status_display=display, github_svc=github_svc),
    )
    assert isinstance(result, ExitFailure)
    assert result.code == 1
    assert result.message is not None
    assert "push" in result.message
    assert "3" in result.message
    assert _printed_messages(display) == []
    github_svc.search_open_issues_by_title.assert_called_once()


# ── MergeCloseFailure ─────────────────────────────────────────────────────────


def test_route_outcome_merge_close_failure_returns_break_loop_with_filed_numbers():
    display = RecordingStatusDisplay()
    result = route_outcome(
        MergeCloseFailure(filed_issue_numbers=[10, 20]),
        _make_deps(status_display=display),
    )
    assert isinstance(result, BreakLoop)
    assert result.message is not None
    assert "#10" in result.message
    assert "#20" in result.message
    assert _printed_messages(display) == []


# ── AbortedSetup ──────────────────────────────────────────────────────────────


def test_route_outcome_aborted_setup_returns_exit_failure_code_1():
    filer = RecordingBugFiler(return_url=None)
    outcome = AbortedSetup(phase="git", message="repository not found")
    result = route_outcome(outcome, _make_deps(bug_filer=filer))
    assert isinstance(result, ExitFailure)
    assert result.code == 1


def test_route_outcome_aborted_setup_message_contains_phase_and_error():
    filer = RecordingBugFiler(return_url=None)
    outcome = AbortedSetup(phase="git", message="repository not found")
    result = route_outcome(outcome, _make_deps(bug_filer=filer))
    assert isinstance(result, ExitFailure)
    assert result.message is not None
    assert "git setup failed: repository not found" in result.message


def test_route_outcome_aborted_setup_message_no_command_line_when_absent():
    filer = RecordingBugFiler(return_url=None)
    outcome = AbortedSetup(phase="git", message="repository not found")
    result = route_outcome(outcome, _make_deps(bug_filer=filer))
    assert isinstance(result, ExitFailure)
    assert result.message is not None
    assert "Command:" not in result.message


def test_route_outcome_aborted_setup_message_no_output_line_when_absent():
    filer = RecordingBugFiler(return_url=None)
    outcome = AbortedSetup(phase="git", message="repository not found")
    result = route_outcome(outcome, _make_deps(bug_filer=filer))
    assert isinstance(result, ExitFailure)
    assert result.message is not None
    assert "Output:" not in result.message


def test_route_outcome_aborted_setup_message_includes_command_when_present():
    filer = RecordingBugFiler(return_url=None)
    outcome = AbortedSetup(
        phase="clone",
        message="exit code 128",
        command="git clone https://example.com/repo.git",
    )
    result = route_outcome(outcome, _make_deps(bug_filer=filer))
    assert isinstance(result, ExitFailure)
    assert result.message is not None
    assert "Command: git clone https://example.com/repo.git" in result.message


def test_route_outcome_aborted_setup_message_includes_output_when_present():
    filer = RecordingBugFiler(return_url=None)
    outcome = AbortedSetup(
        phase="install", message="pip failed", output="error: package not found"
    )
    result = route_outcome(outcome, _make_deps(bug_filer=filer))
    assert isinstance(result, ExitFailure)
    assert result.message is not None
    assert "Output: error: package not found" in result.message


def test_route_outcome_aborted_setup_message_includes_both_command_and_output():
    filer = RecordingBugFiler(return_url=None)
    outcome = AbortedSetup(
        phase="build",
        message="make failed",
        command="make all",
        output="undefined reference to main",
    )
    result = route_outcome(outcome, _make_deps(bug_filer=filer))
    assert isinstance(result, ExitFailure)
    assert result.message is not None
    assert "Command: make all" in result.message
    assert "Output: undefined reference to main" in result.message


def test_route_outcome_aborted_setup_message_appends_report_url_when_filer_returns_one():
    url = "https://github.com/owner/repo/issues/42"
    filer = RecordingBugFiler(return_url=url)
    outcome = AbortedSetup(phase="git", message="repository not found")
    result = route_outcome(outcome, _make_deps(bug_filer=filer))
    assert isinstance(result, ExitFailure)
    assert result.message is not None
    assert f"\nReport: {url}" in result.message


def test_route_outcome_aborted_setup_message_no_report_suffix_when_filer_returns_none():
    filer = RecordingBugFiler(return_url=None)
    outcome = AbortedSetup(phase="git", message="repository not found")
    result = route_outcome(outcome, _make_deps(bug_filer=filer))
    assert isinstance(result, ExitFailure)
    assert result.message is not None
    assert "Report:" not in result.message


def test_route_outcome_aborted_setup_message_no_report_suffix_when_filer_returns_empty_string():
    filer = RecordingBugFiler(return_url="")
    outcome = AbortedSetup(phase="git", message="repository not found")
    result = route_outcome(outcome, _make_deps(bug_filer=filer))
    assert isinstance(result, ExitFailure)
    assert result.message is not None
    assert "Report:" not in result.message


def test_route_outcome_aborted_setup_filer_called_with_phase_title():
    filer = RecordingBugFiler(return_url=None)
    outcome = AbortedSetup(phase="git", message="repository not found")
    route_outcome(outcome, _make_deps(bug_filer=filer))
    assert len(filer.calls) == 1
    title, _, _ = filer.calls[0]
    assert title == "[pycastle] git setup failure: repository not found"


def test_route_outcome_aborted_setup_title_uses_first_line_of_multiline_message():
    filer = RecordingBugFiler(return_url=None)
    outcome = AbortedSetup(phase="git", message="first line\nsecond line\nthird line")
    route_outcome(outcome, _make_deps(bug_filer=filer))
    title, _, _ = filer.calls[0]
    assert title == "[pycastle] git setup failure: first line"


def test_route_outcome_aborted_setup_filer_called_with_body_phase_and_message():
    filer = RecordingBugFiler(return_url=None)
    outcome = AbortedSetup(phase="git", message="repository not found")
    route_outcome(outcome, _make_deps(bug_filer=filer))
    _, body, _ = filer.calls[0]
    assert "## Setup phase failure" in body
    assert "Phase: git" in body
    assert "repository not found" in body


def test_route_outcome_aborted_setup_filer_body_no_command_section_when_absent():
    filer = RecordingBugFiler(return_url=None)
    outcome = AbortedSetup(phase="git", message="repository not found")
    route_outcome(outcome, _make_deps(bug_filer=filer))
    _, body, _ = filer.calls[0]
    assert "Command:" not in body


def test_route_outcome_aborted_setup_filer_body_no_output_section_when_absent():
    filer = RecordingBugFiler(return_url=None)
    outcome = AbortedSetup(phase="git", message="repository not found")
    route_outcome(outcome, _make_deps(bug_filer=filer))
    _, body, _ = filer.calls[0]
    assert "Output:" not in body


def test_route_outcome_aborted_setup_filer_body_includes_command_block():
    filer = RecordingBugFiler(return_url=None)
    outcome = AbortedSetup(
        phase="clone",
        message="exit code 128",
        command="git clone https://example.com/repo.git",
    )
    route_outcome(outcome, _make_deps(bug_filer=filer))
    _, body, _ = filer.calls[0]
    assert "Command: `git clone https://example.com/repo.git`" in body


def test_route_outcome_aborted_setup_filer_body_includes_output_block():
    filer = RecordingBugFiler(return_url=None)
    outcome = AbortedSetup(
        phase="install", message="pip failed", output="error: package not found"
    )
    route_outcome(outcome, _make_deps(bug_filer=filer))
    _, body, _ = filer.calls[0]
    assert "Output:\n\n```\nerror: package not found\n```\n" in body


def test_route_outcome_aborted_setup_filer_called_with_bug_report_labels():
    from pycastle.bug_reporter import BUG_REPORT_LABEL_LIST

    filer = RecordingBugFiler(return_url=None)
    outcome = AbortedSetup(phase="git", message="failed")
    route_outcome(outcome, _make_deps(bug_filer=filer))
    _, _, labels = filer.calls[0]
    assert labels == BUG_REPORT_LABEL_LIST


def test_route_outcome_aborted_setup_display_not_printed_directly():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(phase="git", message="repository not found")
    route_outcome(outcome, _make_deps(bug_filer=filer, status_display=display))
    assert _printed_messages(display) == []


# ── AbortedUsageLimit via route_outcome ───────────────────────────────────────


def _make_service(
    *,
    available: bool,
    wake_time: datetime | None = None,
    permanently_exhausted: bool = False,
) -> MagicMock:
    service = MagicMock(spec=AgentService)
    service.is_available.return_value = available
    if permanently_exhausted:
        service.next_wake_time.side_effect = RuntimeError("no finite wake time")
    elif wake_time is not None:
        service.next_wake_time.return_value = wake_time
    return service


def _stage_override(service: str, fallback_service: str | None = None) -> StageOverride:
    return StageOverride(
        service=service,
        fallback=(
            None
            if fallback_service is None
            else StageOverride(service=fallback_service)
        ),
    )


def _route_usage_limit(
    outcome: AbortedUsageLimit,
    *,
    stage_override: StageOverride | None,
    service_registry: ServiceRegistry | None,
    now: datetime,
) -> ContinueLoop | SleepThenContinue | BreakLoop:
    if stage_override is not None:
        cfg = Config(plan_override=stage_override)
        outcome = dataclasses.replace(outcome, stage_key="plan")
    else:
        cfg = Config()
    deps = _make_deps(cfg=cfg, service_registry=service_registry, now=now)
    result = route_outcome(outcome, deps)
    assert isinstance(result, (ContinueLoop, SleepThenContinue, BreakLoop))
    return result  # type: ignore[return-value]


def test_route_outcome_usage_limit_returns_continue_now_for_stage_fallback():
    primary_wake = datetime(2026, 1, 1, 16, 0, 0, tzinfo=UTC)
    registry = ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=primary_wake),
            "codex": _make_service(available=True),
            "opencode": _make_service(available=False, wake_time=_now()),
        }
    )

    result = _route_usage_limit(
        AbortedUsageLimit(),
        stage_override=_stage_override("claude", "codex"),
        service_registry=registry,
        now=_now(),
    )

    assert isinstance(result, ContinueLoop)


def test_route_outcome_usage_limit_includes_same_day_switch_message():
    primary_wake = datetime(2026, 1, 1, 16, 0, 0, tzinfo=UTC)
    registry = ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=primary_wake),
            "codex": _make_service(available=True),
        }
    )

    result = _route_usage_limit(
        AbortedUsageLimit(),
        stage_override=_stage_override("claude", "codex"),
        service_registry=registry,
        now=_now(),
    )

    assert result == ContinueLoop(
        message="Account exhausted until 16:00, switching to next available.",
    )


def test_route_outcome_usage_limit_formats_same_local_day_switch_message():
    eastern = timezone(timedelta(hours=-5))
    now = datetime(2026, 1, 1, 20, 30, 0, tzinfo=eastern)
    primary_wake = datetime(2026, 1, 2, 1, 0, 0, tzinfo=UTC)
    registry = ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=primary_wake),
            "codex": _make_service(available=True),
        }
    )

    result = _route_usage_limit(
        AbortedUsageLimit(),
        stage_override=_stage_override("claude", "codex"),
        service_registry=registry,
        now=now,
    )

    assert result == ContinueLoop(
        message="Account exhausted until 20:00, switching to next available.",
    )


def test_route_outcome_usage_limit_sleeps_for_stage_chain_only():
    primary_wake = datetime(2026, 1, 1, 16, 0, 0, tzinfo=UTC)
    fallback_wake = datetime(2026, 1, 1, 15, 0, 0, tzinfo=UTC)
    registry = ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=primary_wake),
            "codex": _make_service(available=False, wake_time=fallback_wake),
            "opencode": _make_service(available=True),
        }
    )

    result = _route_usage_limit(
        AbortedUsageLimit(),
        stage_override=_stage_override("claude", "codex"),
        service_registry=registry,
        now=_now(),
    )

    assert isinstance(result, SleepThenContinue)
    assert result.wake_time == fallback_wake
    assert (
        result.message
        == "Usage limit reached. Sleeping until 15:00. Press Ctrl+C to abort."
    )


def test_route_outcome_usage_limit_keeps_failing_service_wake_on_continue_when_other_stage_services_are_exhausted():
    failing_wake = datetime(2026, 1, 1, 16, 0, 0, tzinfo=UTC)
    fallback_wake = datetime(2026, 1, 1, 15, 0, 0, tzinfo=UTC)
    registry = ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=failing_wake),
            "codex": _make_service(available=False, wake_time=fallback_wake),
            "opencode": _make_service(available=True),
        }
    )

    cfg = Config(
        plan_override=StageOverride(
            service="claude",
            fallback=StageOverride(
                service="codex",
                fallback=StageOverride(service="opencode"),
            ),
        )
    )
    result = route_outcome(
        AbortedUsageLimit(provider="claude", stage_key="plan"),
        _make_deps(cfg=cfg, service_registry=registry, now=_now()),
    )

    assert result == ContinueLoop(
        message="Account exhausted until 16:00, switching to next available.",
    )


def test_route_outcome_usage_limit_formats_cross_day_sleep_message():
    now = datetime(2026, 1, 1, 23, 30, 0, tzinfo=UTC)
    fallback_wake = datetime(2026, 1, 2, 1, 0, 0, tzinfo=UTC)
    registry = ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=fallback_wake),
            "codex": _make_service(available=False, wake_time=fallback_wake),
            "opencode": _make_service(available=True),
        }
    )

    result = _route_usage_limit(
        AbortedUsageLimit(),
        stage_override=_stage_override("claude", "codex"),
        service_registry=registry,
        now=now,
    )

    assert isinstance(result, SleepThenContinue)
    assert (
        result.message
        == "Usage limit reached. Sleeping until Jan 2, 01:00. Press Ctrl+C to abort."
    )


def test_route_outcome_usage_limit_formats_same_local_day_sleep_message():
    eastern = timezone(timedelta(hours=-5))
    now = datetime(2026, 1, 1, 20, 30, 0, tzinfo=eastern)
    fallback_wake = datetime(2026, 1, 2, 1, 0, 0, tzinfo=UTC)
    registry = ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=fallback_wake),
            "codex": _make_service(available=False, wake_time=fallback_wake),
        }
    )

    result = _route_usage_limit(
        AbortedUsageLimit(),
        stage_override=_stage_override("claude", "codex"),
        service_registry=registry,
        now=now,
    )

    assert isinstance(result, SleepThenContinue)
    assert (
        result.message
        == "Usage limit reached. Sleeping until 20:00. Press Ctrl+C to abort."
    )


def test_route_outcome_usage_limit_ignores_exhausted_services_outside_stage_chain():
    stage_wake = datetime(2026, 1, 1, 15, 0, 0, tzinfo=UTC)
    unrelated_wake = datetime(2026, 1, 1, 14, 45, 0, tzinfo=UTC)
    registry = ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=stage_wake),
            "codex": _make_service(available=False, wake_time=stage_wake),
            "opencode": _make_service(available=False, wake_time=unrelated_wake),
        }
    )

    result = _route_usage_limit(
        AbortedUsageLimit(),
        stage_override=_stage_override("claude", "codex"),
        service_registry=registry,
        now=_now(),
    )

    assert isinstance(result, SleepThenContinue)
    assert result.wake_time == stage_wake
    assert (
        result.message
        == "Usage limit reached. Sleeping until 15:00. Press Ctrl+C to abort."
    )


def test_route_outcome_usage_limit_ignores_available_services_outside_stage_chain():
    claude_wake = datetime(2026, 1, 1, 15, 0, 0, tzinfo=UTC)
    registry = ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=claude_wake),
            "codex": _make_service(available=True),
        }
    )

    result = _route_usage_limit(
        AbortedUsageLimit(),
        stage_override=StageOverride(service="missing"),
        service_registry=registry,
        now=_now(),
    )

    assert isinstance(result, SleepThenContinue)
    assert result.wake_time == datetime(2026, 1, 1, 15, 2, 0, tzinfo=UTC)
    assert (
        result.message == "Usage limit reached. Sleeping until 15:02 (estimated)."
        " Press Ctrl+C to abort."
    )


def test_route_outcome_usage_limit_uses_global_fallback_when_stage_priority_chain_is_missing():
    primary_wake = datetime(2026, 1, 1, 16, 0, 0, tzinfo=UTC)
    registry = ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=primary_wake),
            "codex": _make_service(available=True),
        }
    )

    result = _route_usage_limit(
        AbortedUsageLimit(),
        stage_override=None,
        service_registry=registry,
        now=_now(),
    )

    assert result == ContinueLoop(
        message="Account exhausted until 16:00, switching to next available.",
    )


def test_route_outcome_usage_limit_uses_global_next_wake_when_stage_priority_chain_is_missing():
    primary_wake = datetime(2026, 1, 1, 16, 0, 0, tzinfo=UTC)
    fallback_wake = datetime(2026, 1, 1, 15, 0, 0, tzinfo=UTC)
    registry = ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=primary_wake),
            "codex": _make_service(available=False, wake_time=fallback_wake),
        }
    )

    result = _route_usage_limit(
        AbortedUsageLimit(),
        stage_override=None,
        service_registry=registry,
        now=_now(),
    )

    assert result == SleepThenContinue(
        wake_time=fallback_wake,
        message="Usage limit reached. Sleeping until 15:00. Press Ctrl+C to abort.",
    )


def test_route_outcome_usage_limit_stops_on_permanent_exhaustion():
    registry = ServiceRegistry(
        {"claude": _make_service(available=False, permanently_exhausted=True)}
    )

    result = _route_usage_limit(
        AbortedUsageLimit(is_permanent=True),
        stage_override=StageOverride(service="claude"),
        service_registry=registry,
        now=_now(),
    )

    assert result == BreakLoop(
        message=(
            "claude unknown account retired for this run and will be retried on the "
            "next run."
        )
    )


def test_route_outcome_usage_limit_returns_continue_now_for_permanent_exhaustion_with_fallback():
    denial = "disabled Claude subscription access for Claude Code"
    primary_wake = datetime(2026, 1, 1, 16, 0, 0, tzinfo=UTC)
    registry = ServiceRegistry(
        {
            "claude": _make_service(available=False, wake_time=primary_wake),
            "codex": _make_service(available=True),
        }
    )

    result = _route_usage_limit(
        AbortedUsageLimit(
            is_permanent=True,
            provider="claude",
            account_label="secondary",
            raw_message=denial,
        ),
        stage_override=_stage_override("claude", "codex"),
        service_registry=registry,
        now=_now(),
    )

    assert result == ContinueLoop(
        message=(
            "claude secondary account retired for this run and will be retried on "
            "the next run. Claude said: disabled Claude subscription access for "
            "Claude Code"
        ),
    )


def test_route_outcome_usage_limit_stops_on_permanent_exhaustion_without_configured_fallback():
    denial = "disabled Claude subscription access for Claude Code"
    registry = ServiceRegistry(
        {
            "claude": _make_service(available=False, permanently_exhausted=True),
            "codex": _make_service(available=True),
        }
    )

    result = _route_usage_limit(
        AbortedUsageLimit(
            is_permanent=True,
            provider="claude",
            account_label="primary",
            raw_message=denial,
        ),
        stage_override=StageOverride(service="claude"),
        service_registry=registry,
        now=_now(),
    )

    assert result == BreakLoop(
        message=(
            "claude primary account retired for this run and will be retried on "
            "the next run. Claude said: disabled Claude subscription access for "
            "Claude Code"
        )
    )


def test_route_outcome_usage_limit_uses_observed_provider_label_for_non_claude_permanent_message():
    registry = ServiceRegistry(
        {"opencode": _make_service(available=False, permanently_exhausted=True)}
    )

    result = _route_usage_limit(
        AbortedUsageLimit(
            is_permanent=True,
            provider="OpenCode",
            account_label="primary",
            raw_message="usage limit reached for this account",
        ),
        stage_override=StageOverride(service="opencode"),
        service_registry=registry,
        now=_now(),
    )

    assert result == BreakLoop(
        message=(
            "OpenCode primary account retired for this run and will be retried on "
            "the next run. OpenCode said: usage limit reached for this account"
        )
    )


def test_route_outcome_usage_limit_estimates_wake_time_without_registry():
    now = _now()

    result = _route_usage_limit(
        AbortedUsageLimit(reset_time=None),
        stage_override=None,
        service_registry=None,
        now=now,
    )

    assert isinstance(result, SleepThenContinue)
    assert result.wake_time == datetime(2026, 1, 1, 15, 2, 0, tzinfo=UTC)
    assert (
        result.message == "Usage limit reached. Sleeping until 15:02 (estimated)."
        " Press Ctrl+C to abort."
    )


def test_route_outcome_usage_limit_uses_exact_reset_time_without_registry():
    now = _now()
    reset_time = datetime(2026, 1, 1, 15, 30, 0, tzinfo=UTC)

    result = _route_usage_limit(
        AbortedUsageLimit(reset_time=reset_time),
        stage_override=None,
        service_registry=None,
        now=now,
    )

    assert isinstance(result, SleepThenContinue)
    assert result.wake_time == datetime(2026, 1, 1, 15, 32, 0, tzinfo=UTC)
    assert (
        result.message
        == "Usage limit reached. Sleeping until 15:32. Press Ctrl+C to abort."
    )


def test_route_outcome_usage_limit_formats_cross_day_exact_reset_without_registry():
    now = datetime(2026, 1, 1, 23, 30, 0, tzinfo=UTC)
    reset_time = datetime(2026, 1, 2, 0, 30, 0, tzinfo=UTC)

    result = _route_usage_limit(
        AbortedUsageLimit(reset_time=reset_time),
        stage_override=None,
        service_registry=None,
        now=now,
    )

    assert isinstance(result, SleepThenContinue)
    assert (
        result.message
        == "Usage limit reached. Sleeping until Jan 2, 00:32. Press Ctrl+C to abort."
    )


def test_route_outcome_usage_limit_keeps_stage_key_behavior_without_registry():
    now = _now()

    result = _route_usage_limit(
        AbortedUsageLimit(reset_time=None),
        stage_override=StageOverride(service="claude"),
        service_registry=None,
        now=now,
    )

    assert isinstance(result, SleepThenContinue)
    assert result.wake_time == datetime(2026, 1, 1, 15, 2, 0, tzinfo=UTC)
    assert (
        result.message == "Usage limit reached. Sleeping until 15:02 (estimated)."
        " Press Ctrl+C to abort."
    )


def test_route_outcome_usage_limit_uses_provider_minimum_duration_for_unknown_reset():
    result = route_outcome(
        AbortedUsageLimit(
            provider="codex",
            reset_time=None,
            stage_key="review",
        ),
        _make_deps(
            cfg=Config(codex_minimum_unknown_reset_duration_hours=1.5),
            service_registry=None,
            now=_now(),
        ),
    )

    assert isinstance(result, SleepThenContinue)
    assert result.wake_time == datetime(2026, 1, 1, 16, 2, 0, tzinfo=UTC)
    assert (
        result.message == "Usage limit reached. Sleeping until 16:02 (estimated)."
        " Press Ctrl+C to abort."
    )


def test_route_outcome_usage_limit_defaults_opencode_unknown_reset_to_one_hour():
    result = route_outcome(
        AbortedUsageLimit(
            provider="opencode",
            reset_time=None,
            stage_key="plan",
        ),
        _make_deps(cfg=Config(), service_registry=None, now=_now()),
    )

    assert isinstance(result, SleepThenContinue)
    assert result.wake_time == datetime(2026, 1, 1, 16, 2, 0, tzinfo=UTC)
    assert (
        result.message == "Usage limit reached. Sleeping until 16:02 (estimated)."
        " Press Ctrl+C to abort."
    )


def test_route_outcome_usage_limit_explicit_zero_opencode_unknown_reset_uses_next_hour():
    result = route_outcome(
        AbortedUsageLimit(
            provider="opencode",
            reset_time=None,
            stage_key="plan",
        ),
        _make_deps(
            cfg=Config(opencode_minimum_unknown_reset_duration_hours=0.0),
            service_registry=None,
            now=_now(),
        ),
    )

    assert isinstance(result, SleepThenContinue)
    assert result.wake_time == datetime(2026, 1, 1, 15, 2, 0, tzinfo=UTC)
    assert (
        result.message == "Usage limit reached. Sleeping until 15:02 (estimated)."
        " Press Ctrl+C to abort."
    )


def test_route_outcome_usage_limit_keeps_parsed_reset_time_authoritative():
    reset_time = datetime(2026, 1, 1, 15, 30, 0, tzinfo=UTC)

    result = route_outcome(
        AbortedUsageLimit(
            provider="codex",
            reset_time=reset_time,
            stage_key="review",
        ),
        _make_deps(
            cfg=Config(codex_minimum_unknown_reset_duration_hours=6),
            service_registry=None,
            now=_now(),
        ),
    )

    assert isinstance(result, SleepThenContinue)
    assert result.wake_time == datetime(2026, 1, 1, 15, 32, 0, tzinfo=UTC)
    assert (
        result.message
        == "Usage limit reached. Sleeping until 15:32. Press Ctrl+C to abort."
    )


def test_route_outcome_usage_limit_sleeps_when_permanently_exhausted_but_other_candidate_has_finite_wake_time():
    fallback_wake = datetime(2026, 1, 1, 16, 0, 0, tzinfo=UTC)
    registry = ServiceRegistry(
        {
            "claude": _make_service(available=False, permanently_exhausted=True),
            "codex": _make_service(available=False, wake_time=fallback_wake),
        }
    )

    result = _route_usage_limit(
        AbortedUsageLimit(is_permanent=True, provider="claude"),
        stage_override=_stage_override("claude", "codex"),
        service_registry=registry,
        now=_now(),
    )

    assert isinstance(result, SleepThenContinue)
    assert result.wake_time == fallback_wake


def test_route_outcome_usage_limit_stops_when_all_chain_candidates_permanently_exhausted():
    registry = ServiceRegistry(
        {
            "claude": _make_service(available=False, permanently_exhausted=True),
            "codex": _make_service(available=False, permanently_exhausted=True),
        }
    )

    result = _route_usage_limit(
        AbortedUsageLimit(is_permanent=True, provider="claude"),
        stage_override=_stage_override("claude", "codex"),
        service_registry=registry,
        now=_now(),
    )

    assert isinstance(result, BreakLoop)


def test_route_outcome_usage_limit_permanent_no_registry_returns_break_loop():
    result = route_outcome(
        AbortedUsageLimit(is_permanent=True),
        _make_deps(cfg=Config(), service_registry=None, now=_now()),
    )
    assert isinstance(result, BreakLoop)


def test_route_outcome_usage_limit_temporary_no_registry_returns_sleep_then_continue():
    reset = _now() + timedelta(hours=2)
    result = route_outcome(
        AbortedUsageLimit(is_permanent=False, reset_time=reset, provider="claude"),
        _make_deps(cfg=Config(), service_registry=None, now=_now()),
    )
    assert isinstance(result, SleepThenContinue)
    assert result.wake_time > _now()
    assert "Sleeping until" in result.message


def test_route_outcome_usage_limit_with_fallback_service_returns_continue_loop():
    available_svc = MagicMock(spec=AgentService)
    available_svc.is_available.return_value = True
    registry = ServiceRegistry({"codex": available_svc})

    result = route_outcome(
        AbortedUsageLimit(is_permanent=False, provider="claude", stage_key="plan"),
        _make_deps(cfg=Config(), service_registry=registry, now=_now()),
    )
    assert result == ContinueLoop()


# ── AbortedModelNotAvailable via route_outcome ────────────────────────────────


def test_route_outcome_model_not_available_no_registry_returns_break_loop():
    result = route_outcome(
        AbortedModelNotAvailable(service="codex", model="gpt-5.3"),
        _make_deps(cfg=Config(), service_registry=None, now=_now()),
    )
    assert isinstance(result, BreakLoop)
    assert result.message is not None
    assert "gpt-5.3" in result.message


def test_route_outcome_model_not_available_with_wake_time_returns_sleep_then_continue():
    wake = _now() + timedelta(hours=1)
    unavailable_svc = MagicMock(spec=AgentService)
    unavailable_svc.is_available.return_value = False
    unavailable_svc.next_wake_time.return_value = wake
    registry = ServiceRegistry({"codex": unavailable_svc})

    result = route_outcome(
        AbortedModelNotAvailable(service="codex", model="gpt-5.3"),
        _make_deps(cfg=Config(), service_registry=registry, now=_now()),
    )
    assert isinstance(result, SleepThenContinue)
    assert result.wake_time == wake


def test_route_outcome_model_not_available_with_available_service_returns_continue_loop():
    available_svc = MagicMock(spec=AgentService)
    available_svc.is_available.return_value = True
    registry = ServiceRegistry({"claude": available_svc})

    result = route_outcome(
        AbortedModelNotAvailable(service="codex", model="gpt-5.3", stage_key="plan"),
        _make_deps(cfg=Config(), service_registry=registry, now=_now()),
    )
    assert result == ContinueLoop()
