"""Interface-level tests for iteration.failure_report_dispatch.

Tests verify that translate_agent_failed_error_to_abort produces the correct return
values and side effects for every observable behaviour of the extracted translator:
diagnose gate, DiagnosticMountFallbackIssue filing, RunRequest construction, evidence
copy (ADR-0035), crash-funnel exception logging, credential-failure routing, and
non-agent exception propagation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path, PureWindowsPath
from unittest.mock import MagicMock, patch

import pytest
from agent_runtime.errors import AgentCredentialFailureError

from pycastle.agent_credential_failure_routing import AgentCredentialFailureRouteResult
from pycastle.agents.output_protocol import AgentRole, IssueOutput
from pycastle.config import Config, StageOverride
from pycastle.errors import (
    AgentFailedError,
    AgentTimeoutError,
)
from pycastle.iteration import (
    AbortedAgentCredentialFailure,
    AbortedAgentFailure,
    failure_report_dispatch,
)
from pycastle.iteration.failure_report_dispatch import (
    translate_agent_failed_error_to_abort,
)
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.services import GithubService
from tests.support import (
    FakeAgentRunner,
    RecordingLogger,
    RecordingStatusDisplay,
    _make_deps,
)

# ── Test doubles ──────────────────────────────────────────────────────────────


def _worktree_path(tmp_path: Path, name: str = "improve-sandbox") -> Path:
    return tmp_path / "pycastle" / ".worktrees" / name


def _make_valid_worktree(tmp_path: Path, name: str = "improve-sandbox") -> Path:
    path = _worktree_path(tmp_path, name)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _make_err(
    tmp_path: Path,
    *,
    role_value: str = "improve",
    worktree_name: str = "improve-sandbox",
    failure_class: str = "protocol_error",
    service_name: str = "codex",
) -> AgentFailedError:
    return AgentFailedError(
        role_value=role_value,
        worktree_path=_worktree_path(tmp_path, worktree_name),
        failure_class=failure_class,
        service_name=service_name,
    )


@pytest.fixture
def logger() -> RecordingLogger:
    return RecordingLogger()


# ── diagnose_on_failure gate ──────────────────────────────────────────────────


def test_diagnose_off_short_circuits_without_dispatching_any_agent(tmp_path, logger):
    """diagnose_on_failure=False returns AbortedAgentFailure(issue_number=None)
    immediately without running the Failure-Report agent."""
    err = _make_err(tmp_path)
    runner = FakeAgentRunner([])
    deps = _make_deps(
        tmp_path, runner, cfg=Config(diagnose_on_failure=False), logger=logger
    )

    result = asyncio.run(translate_agent_failed_error_to_abort(err, deps))

    assert isinstance(result, AbortedAgentFailure)
    assert result.failed_role == "improve"
    assert result.issue_number is None
    assert runner.calls == []


# ── DiagnosticMountFallbackIssue ─────────────────────────────────────────────


def test_invalid_mount_files_fallback_issue_and_skips_failure_report_agent(
    tmp_path, logger
):
    """An invalid managed-worktree mount returns AbortedAgentFailure carrying the
    fallback issue number without spawning the Failure-Report agent."""
    invalid_mount = tmp_path / "outside-worktrees" / "improve-sandbox"
    invalid_mount.mkdir(parents=True, exist_ok=True)
    # expected_worktrees_dir must exist so should_reject_managed_worktree_mount returns True
    (tmp_path / "pycastle" / ".worktrees").mkdir(parents=True, exist_ok=True)

    err = AgentFailedError(
        role_value="improve",
        worktree_path=invalid_mount,
        failure_class="protocol_error",
        service_name="codex",
    )

    github_svc = MagicMock(spec=GithubService)
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []
    github_svc.create_issue_in.return_value = (321, 10321)

    runner = FakeAgentRunner([])
    deps = _make_deps(tmp_path, runner, github_svc=github_svc, logger=logger)

    result = asyncio.run(translate_agent_failed_error_to_abort(err, deps))

    assert isinstance(result, AbortedAgentFailure)
    assert result.failed_role == "improve"
    assert result.issue_number == 321
    assert runner.calls == []
    github_svc.create_issue_in.assert_called_once()
    repo, title, body, labels = github_svc.create_issue_in.call_args.args
    assert repo == github_svc.repo
    assert "Failure Report Agent" in title
    assert labels == ["bug", "needs-triage"]
    assert "No diagnostic agent ran." in body
    assert "Role: improve" in body
    assert f"Expected mount path: {_worktree_path(tmp_path)}" in body
    assert "Reason: invalid_mount_path" in body


# ── RunRequest construction ───────────────────────────────────────────────────


def test_run_request_carries_failure_report_role_template_path_and_service(
    tmp_path, logger
):
    """The Failure-Report RunRequest carries AgentRole.FAILURE_REPORT,
    PromptTemplate.FAILURE_REPORT, the failing worktree path, and the configured
    preflight_issue_override.service."""
    expected_path = _make_valid_worktree(tmp_path)
    err = AgentFailedError(
        role_value="improve",
        worktree_path=expected_path,
        failure_class="protocol_error",
        service_name="codex",
    )

    runner = FakeAgentRunner([IssueOutput(number=99, labels=["bug"])])
    deps = _make_deps(
        tmp_path,
        runner,
        logger=logger,
        cfg=Config(
            preflight_issue_override=StageOverride(service="codex", effort="medium")
        ),
    )

    result = asyncio.run(translate_agent_failed_error_to_abort(err, deps))

    assert len(runner.calls) == 1
    req = runner.calls[0]
    assert req.role == AgentRole.FAILURE_REPORT
    assert req.prompt.template == PromptTemplate.FAILURE_REPORT
    assert req.service == "codex"
    assert req.mount_path == expected_path
    assert isinstance(result, AbortedAgentFailure)
    assert result.issue_number == 99


# ── Evidence path (ADR-0035) ──────────────────────────────────────────────────


def test_evidence_file_is_copied_to_adr_0035_relative_path(tmp_path, logger):
    """The evidence file is present at the ADR-0035 relative path after a
    successful copy and the scope args reflect the path."""
    expected_path = _make_valid_worktree(tmp_path)
    source_log = tmp_path / "captured-agent.log"
    source_log.write_bytes(
        b'{"type":"result","result":"attempt-1"}\n'
        b'{"type":"result","result":"attempt-2"}\n'
    )

    err = AgentFailedError(
        role_value="improve",
        worktree_path=expected_path,
        failure_class="protocol_error",
        service_name="codex",
    )
    err.agent_invocation_log_path = source_log

    runner = FakeAgentRunner([IssueOutput(number=99, labels=["bug"])])
    deps = _make_deps(
        tmp_path,
        runner,
        logger=logger,
        cfg=Config(
            preflight_issue_override=StageOverride(service="codex", effort="medium")
        ),
    )

    asyncio.run(translate_agent_failed_error_to_abort(err, deps))

    req = runner.calls[0]
    evidence_path = req.prompt.scope_args["EVIDENCE_PATH"]
    has_evidence = req.prompt.scope_args["HAS_EVIDENCE_PATH"]
    assert evidence_path == ".pycastle-session/failure-report/agent-invocation.log"
    assert has_evidence == "yes"

    copied_log = expected_path / evidence_path
    assert copied_log.exists()
    assert copied_log.read_bytes() == source_log.read_bytes()


def test_evidence_path_scope_arg_normalises_to_posix_from_windows_shaped_constant(
    tmp_path, logger, monkeypatch
):
    """The evidence-path scope arg normalises to POSIX form even when
    _EVIDENCE_DIR holds a Windows-shaped path."""
    expected_path = _make_valid_worktree(tmp_path)
    source_log = tmp_path / "captured-agent.log"
    source_log.write_bytes(b'{"type":"result","result":"attempt-1"}\n')

    err = AgentFailedError(
        role_value="improve",
        worktree_path=expected_path,
        failure_class="protocol_error",
        service_name="codex",
    )
    err.agent_invocation_log_path = source_log

    monkeypatch.setattr(
        failure_report_dispatch,
        "_EVIDENCE_DIR",
        PureWindowsPath(".pycastle-session") / "failure-report",
    )

    runner = FakeAgentRunner([IssueOutput(number=99, labels=["bug"])])
    deps = _make_deps(
        tmp_path,
        runner,
        logger=logger,
        cfg=Config(
            preflight_issue_override=StageOverride(service="codex", effort="medium")
        ),
    )

    asyncio.run(translate_agent_failed_error_to_abort(err, deps))

    assert (
        runner.calls[0].prompt.scope_args["EVIDENCE_PATH"]
        == ".pycastle-session/failure-report/agent-invocation.log"
    )


def test_missing_source_log_dispatches_with_empty_evidence_path(tmp_path, logger):
    """A missing source log dispatches with EVIDENCE_PATH="" and
    HAS_EVIDENCE_PATH="no"."""
    expected_path = _make_valid_worktree(tmp_path)
    missing_log = tmp_path / "missing" / "agent-invocation.log"

    err = AgentFailedError(
        role_value="improve",
        worktree_path=expected_path,
        failure_class="protocol_error",
        service_name="codex",
    )
    err.agent_invocation_log_path = missing_log

    runner = FakeAgentRunner([IssueOutput(number=99, labels=["bug"])])
    deps = _make_deps(
        tmp_path,
        runner,
        logger=logger,
        cfg=Config(
            preflight_issue_override=StageOverride(service="codex", effort="medium")
        ),
    )

    asyncio.run(translate_agent_failed_error_to_abort(err, deps))

    req = runner.calls[0]
    assert req.prompt.scope_args["HAS_EVIDENCE_PATH"] == "no"
    assert req.prompt.scope_args["EVIDENCE_PATH"] == ""


def test_missing_worktree_mount_does_not_materialize_evidence_dir(tmp_path, logger):
    """When the worktree mount path is absent (already gone before the error
    propagated), the evidence copy is skipped and the RunRequest carries empty
    EVIDENCE_PATH."""
    # Path whose parent is not in the managed worktrees dir and does not exist —
    # decide_diagnostic_mount_dispatch accepts it (should_reject returns False)
    # but _copy_invocation_log_to_evidence_area skips the copy.
    missing_mount = tmp_path / "never-existed-worktree"
    source_log = tmp_path / "captured-agent.log"
    source_log.write_text("captured bytes", encoding="utf-8")

    err = AgentFailedError(
        role_value="improve",
        worktree_path=missing_mount,
        failure_class="protocol_error",
        service_name="codex",
        agent_invocation_log_path=source_log,
    )
    report_crash = AgentTimeoutError("missing mount still rejected")

    runner = FakeAgentRunner([report_crash])
    deps = _make_deps(
        tmp_path,
        runner,
        logger=logger,
        cfg=Config(
            preflight_issue_override=StageOverride(service="codex", effort="medium")
        ),
    )

    result = asyncio.run(translate_agent_failed_error_to_abort(err, deps))

    assert isinstance(result, AbortedAgentFailure)
    assert result.failed_role == "improve"
    assert result.issue_number is None
    assert not missing_mount.exists()
    assert len(runner.calls) == 1
    assert runner.calls[0].prompt.scope_args["HAS_EVIDENCE_PATH"] == "no"
    assert runner.calls[0].prompt.scope_args["EVIDENCE_PATH"] == ""


# ── Crash funnel ──────────────────────────────────────────────────────────────


def test_crash_funnel_logs_warning_and_internal_error_and_returns_aborted(
    tmp_path, logger
):
    """When the Failure-Report agent crashes with an exception in the funnel,
    a status-display warning is printed, the error is logged via
    deps.logger.log_internal_error, and AbortedAgentFailure(issue_number=None) is returned."""
    expected_path = _make_valid_worktree(tmp_path)
    original_error = AgentFailedError(
        role_value="improve",
        worktree_path=expected_path,
        failure_class="protocol_error",
        service_name="codex",
    )
    report_crash = AgentTimeoutError(
        role_value="failure-report", worktree_path=tmp_path
    )

    runner = FakeAgentRunner([report_crash])
    status = RecordingStatusDisplay()
    deps = _make_deps(tmp_path, runner, logger=logger, status_display=status)

    result = asyncio.run(translate_agent_failed_error_to_abort(original_error, deps))

    assert isinstance(result, AbortedAgentFailure)
    assert result.issue_number is None

    prints = [(c[1], c[2]) for c in status.calls if c[0] == "print"]
    assert any("Failure-Report agent crashed" in str(msg) for _, msg in prints)

    assert len(logger.internal_errors) == 1
    label, logged_error, logged_cause = logger.internal_errors[0]
    assert "role=improve" in label
    assert logged_error is report_crash
    assert logged_cause is original_error


def test_non_agent_exception_propagates_through_crash_funnel(tmp_path, logger):
    """A non-agent exception raised by the Failure-Report agent propagates
    instead of being swallowed by the crash funnel."""
    expected_path = _make_valid_worktree(tmp_path)
    original_error = AgentFailedError(
        role_value="improve",
        worktree_path=expected_path,
        failure_class="protocol_error",
        service_name="codex",
    )
    report_crash = RuntimeError("unexpected crash in failure reporter")

    runner = FakeAgentRunner([report_crash])
    deps = _make_deps(tmp_path, runner, logger=logger)

    with pytest.raises(RuntimeError, match="unexpected crash in failure reporter"):
        asyncio.run(translate_agent_failed_error_to_abort(original_error, deps))


# ── Credential failure routing ────────────────────────────────────────────────


def test_credential_failure_routes_through_shared_terminal_path(tmp_path, logger):
    """A Failure-Report agent credential failure routes through
    route_agent_credential_failure and returns AbortedAgentCredentialFailure(status_code=...)."""
    expected_path = _make_valid_worktree(tmp_path, "issue-1")
    original_error = AgentFailedError(
        role_value="implementer",
        worktree_path=expected_path,
        failure_class="protocol_error",
        service_name="claude",
    )
    credential_error = AgentCredentialFailureError(
        "Credential failure observed while running the Failure-Report agent.",
        service_name="codex",
    )
    credential_error.caller = "Failure Report Agent"

    github_svc = MagicMock(spec=GithubService)
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []

    runner = FakeAgentRunner([credential_error])
    status = RecordingStatusDisplay()
    deps = _make_deps(
        tmp_path,
        runner,
        github_svc=github_svc,
        logger=logger,
        status_display=status,
        cfg=Config(diagnose_on_failure=True),
    )

    with patch(
        "pycastle.iteration.route_agent_credential_failure",
        return_value=AgentCredentialFailureRouteResult(
            status_code=401,
            status_message="operator-actionable agent credential failure: status 401",
            issue_url="https://github.com/owner/consuming-project/issues/88",
        ),
    ) as mock_route:
        result = asyncio.run(
            translate_agent_failed_error_to_abort(original_error, deps)
        )

    assert isinstance(result, AbortedAgentCredentialFailure)
    assert result.status_code == 401
    mock_route.assert_called_once_with(
        provider_failure=credential_error,
        github_svc=github_svc,
    )
    assert any(
        call[0] == "print"
        and "operator-actionable agent credential failure: status 401" in str(call[2])
        and "https://github.com/owner/consuming-project/issues/88" in str(call[2])
        for call in status.calls
    )
    assert logger.internal_errors == []
