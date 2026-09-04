"""Tests for upstream_issue_report — structured upstream issue filing module."""

from __future__ import annotations

import pytest


def _make_github_svc():
    from unittest.mock import MagicMock

    from pycastle.services import GithubService

    svc = MagicMock(spec=GithubService)
    svc.repo = "consumer/owner"
    svc.search_open_issues_by_title.return_value = []
    svc.create_issue_in.return_value = (123, 10123)
    return svc


def _make_report(
    *,
    dedupe_key="[pycastle] test prefix",
    title="[pycastle] test prefix: detail",
    body="body text",
    labels=None,
    github_svc=None,
    status_display=None,
    caller="",
):
    from pycastle.upstream_issue_report import UpstreamIssueReport

    if labels is None:
        labels = ["bug", "needs-triage"]
    if github_svc is None:
        github_svc = _make_github_svc()
    return UpstreamIssueReport(
        dedupe_key=dedupe_key,
        title=title,
        body=body,
        labels=labels,
        github_svc=github_svc,
        status_display=status_display,
        caller=caller,
    )


# ── Constants ─────────────────────────────────────────────────────────────────


def test_bug_and_triage_labels_constant():
    from pycastle.upstream_issue_report import BUG_AND_TRIAGE_LABELS

    assert BUG_AND_TRIAGE_LABELS == ["bug", "needs-triage"]


# ── Dedupe: existing open issue is returned ───────────────────────────────────


def test_returns_existing_issue_number_when_search_matches():
    from pycastle.upstream_issue_report import file_upstream_issue

    svc = _make_github_svc()
    svc.search_open_issues_by_title.return_value = [99]
    report = _make_report(github_svc=svc)

    result = file_upstream_issue(report)

    assert result == 99
    svc.create_issue_in.assert_not_called()


def test_returns_first_match_when_multiple_existing_issues():
    from pycastle.upstream_issue_report import file_upstream_issue

    svc = _make_github_svc()
    svc.search_open_issues_by_title.return_value = [77, 88]
    report = _make_report(github_svc=svc)

    result = file_upstream_issue(report)

    assert result == 77
    svc.create_issue_in.assert_not_called()


# ── Create: no existing issue triggers create ─────────────────────────────────


def test_creates_issue_when_no_existing_match():
    from pycastle.upstream_issue_report import file_upstream_issue

    svc = _make_github_svc()
    report = _make_report(
        title="the title",
        body="the body",
        labels=["bug", "needs-triage"],
        github_svc=svc,
    )

    result = file_upstream_issue(report)

    assert result == 123
    call_args = svc.create_issue_in.call_args
    assert call_args.args[0] == "consumer/owner"
    assert call_args.args[1] == "the title"
    assert call_args.args[3] == ["bug", "needs-triage"]


def test_create_uses_github_svc_repo():
    from pycastle.upstream_issue_report import file_upstream_issue

    svc = _make_github_svc()
    svc.repo = "other-org/other-repo"
    svc.create_issue_in.return_value = (55, 10055)
    report = _make_report(github_svc=svc)

    result = file_upstream_issue(report)

    assert result == 55
    call_repo = svc.create_issue_in.call_args.args[0]
    assert call_repo == "other-org/other-repo"


# ── Env block: filer prepends it to the body ──────────────────────────────────


def test_filer_prepends_env_block_to_body():
    from pycastle.upstream_issue_report import file_upstream_issue

    svc = _make_github_svc()
    report = _make_report(body="caller-supplied body", github_svc=svc)

    file_upstream_issue(report)

    filed_body = svc.create_issue_in.call_args.args[2]
    assert "## Environment" in filed_body
    assert "- pycastle:" in filed_body
    assert "- Python:" in filed_body
    assert "- OS:" in filed_body
    assert "caller-supplied body" in filed_body
    env_pos = filed_body.index("## Environment")
    body_pos = filed_body.index("caller-supplied body")
    assert env_pos < body_pos


# ── Echo mode: click.echo (default) ──────────────────────────────────────────


def test_successful_create_emits_filed_line_via_click_echo(capsys):
    from pycastle.upstream_issue_report import file_upstream_issue

    svc = _make_github_svc()
    report = _make_report(title="My Issue Title", github_svc=svc)

    file_upstream_issue(report)

    out = capsys.readouterr().out
    assert "Filed issue #123 on consumer/owner: My Issue Title" in out


def test_no_echo_when_existing_issue_returned(capsys):
    from pycastle.upstream_issue_report import file_upstream_issue

    svc = _make_github_svc()
    svc.search_open_issues_by_title.return_value = [99]
    report = _make_report(github_svc=svc)

    file_upstream_issue(report)

    out = capsys.readouterr().out
    assert "Filed issue" not in out


# ── Echo mode: StatusDisplay.print ───────────────────────────────────────────


def test_status_display_print_called_instead_of_click_echo(capsys):
    from unittest.mock import MagicMock

    from pycastle.upstream_issue_report import file_upstream_issue

    svc = _make_github_svc()
    status_display = MagicMock()
    report = _make_report(
        title="My Issue Title",
        github_svc=svc,
        status_display=status_display,
        caller="worker",
    )

    file_upstream_issue(report)

    out = capsys.readouterr().out
    assert "Filed issue" not in out
    status_display.print.assert_called_once()
    call_args = status_display.print.call_args
    assert call_args.args[0] == "worker"
    assert "Filed issue #123 on consumer/owner: My Issue Title" in call_args.args[1]


# ── Search error: GithubServiceError treated as no match ─────────────────────


def test_search_github_service_error_proceeds_to_create():
    from pycastle.services import GithubNetworkError
    from pycastle.upstream_issue_report import file_upstream_issue

    svc = _make_github_svc()
    svc.search_open_issues_by_title.side_effect = GithubNetworkError(
        "dns fail", cause=OSError("dns")
    )
    report = _make_report(github_svc=svc)

    result = file_upstream_issue(report)

    assert result == 123
    svc.create_issue_in.assert_called_once()


def test_search_github_api_error_proceeds_to_create():
    from pycastle.services import GithubAPIError
    from pycastle.upstream_issue_report import file_upstream_issue

    svc = _make_github_svc()
    svc.search_open_issues_by_title.side_effect = GithubAPIError(
        "bad", status=503, body="down", method="GET", path="/search"
    )
    report = _make_report(github_svc=svc)

    result = file_upstream_issue(report)

    assert result == 123
    svc.create_issue_in.assert_called_once()


# ── Create error: GithubServiceError returns None ────────────────────────────


def test_create_github_service_error_returns_none():
    from pycastle.services import GithubNetworkError
    from pycastle.upstream_issue_report import file_upstream_issue

    svc = _make_github_svc()
    svc.create_issue_in.side_effect = GithubNetworkError(
        "create failed", cause=OSError("refused")
    )
    report = _make_report(github_svc=svc)

    result = file_upstream_issue(report)

    assert result is None


def test_create_github_api_error_returns_none():
    from pycastle.services import GithubAPIError
    from pycastle.upstream_issue_report import file_upstream_issue

    svc = _make_github_svc()
    svc.create_issue_in.side_effect = GithubAPIError(
        "500", status=500, body="err", method="POST", path="/repos/x/issues"
    )
    report = _make_report(github_svc=svc)

    result = file_upstream_issue(report)

    assert result is None


def test_search_error_then_create_error_returns_none():
    from pycastle.services import GithubNetworkError
    from pycastle.upstream_issue_report import file_upstream_issue

    svc = _make_github_svc()
    svc.search_open_issues_by_title.side_effect = GithubNetworkError(
        "dns fail", cause=OSError("dns")
    )
    svc.create_issue_in.side_effect = GithubNetworkError(
        "create failed", cause=OSError("refused")
    )
    report = _make_report(github_svc=svc)

    result = file_upstream_issue(report)

    assert result is None


# ── Non-GithubServiceError propagates unchanged ───────────────────────────────


def test_search_non_github_error_propagates():
    from pycastle.upstream_issue_report import file_upstream_issue

    svc = _make_github_svc()
    svc.search_open_issues_by_title.side_effect = RuntimeError("unexpected")
    report = _make_report(github_svc=svc)

    with pytest.raises(RuntimeError, match="unexpected"):
        file_upstream_issue(report)


def test_create_non_github_error_propagates():
    from pycastle.upstream_issue_report import file_upstream_issue

    svc = _make_github_svc()
    svc.create_issue_in.side_effect = ValueError("unexpected shape")
    report = _make_report(github_svc=svc)

    with pytest.raises(ValueError, match="unexpected shape"):
        file_upstream_issue(report)


# ── Body composers ────────────────────────────────────────────────────────────


def test_hard_agent_error_body_contains_expected_sections():
    from pycastle.upstream_issue_report import hard_agent_error_body

    body = hard_agent_error_body(
        raw='{"status": 500}',
        effective_status_code=500,
        caller="my-agent",
        service_name="claude",
    )

    assert "## Raw result envelope" in body
    assert '{"status": 500}' in body
    assert "Status: 500" in body
    assert "Agent: my-agent" in body
    assert "Service: claude" in body
    assert "## Environment" not in body


def test_hard_agent_error_body_unknown_caller():
    from pycastle.upstream_issue_report import hard_agent_error_body

    body = hard_agent_error_body(
        raw="",
        effective_status_code=None,
        caller="",
        service_name="claude",
    )

    assert "Agent: <unknown>" in body


def test_aborted_setup_body_contains_expected_sections():
    from pycastle.upstream_issue_report import aborted_setup_body

    body = aborted_setup_body(phase="clone", message="clone failed")

    assert "## Setup phase failure" in body
    assert "Phase: clone" in body
    assert "clone failed" in body
    assert "## Environment" not in body


def test_aborted_setup_body_includes_optional_command_and_output():
    from pycastle.upstream_issue_report import aborted_setup_body

    body = aborted_setup_body(
        phase="install",
        message="pip failed",
        command="pip install -r requirements.txt",
        output="ERROR: ...",
    )

    assert "pip install -r requirements.txt" in body
    assert "ERROR: ..." in body


def test_aborted_setup_body_omits_optional_fields_when_absent():
    from pycastle.upstream_issue_report import aborted_setup_body

    body = aborted_setup_body(phase="clone", message="msg")

    assert "Command:" not in body
    assert "Output:" not in body


def test_merge_close_failure_body_contains_expected_sections():
    from pycastle.upstream_issue_report import merge_close_failure_body

    exc = RuntimeError("close failed")
    body = merge_close_failure_body(issue_number=42, exc=exc)

    assert "## Merge close failure" in body
    assert "issue #42" in body
    assert "close failed" in body
    assert "## Environment" not in body


def test_operator_actionable_body_contains_expected_sections():
    from pycastle.upstream_issue_report import operator_actionable_body

    body = operator_actionable_body(op="git push", stderr="fatal: ...", attempt_count=3)

    assert "git push" in body
    assert "3 attempt(s)" in body
    assert "fatal: ..." in body
    assert "Troubleshooting hints" in body
    assert "## Environment" not in body


def test_unrepairable_draft_body_contains_expected_sections():
    from pycastle.upstream_issue_report import unrepairable_draft_body

    body = unrepairable_draft_body(
        problems=["file.py missing", "syntax error"],
        draft_files={"file.py": "print('hello')"},
    )

    assert "## Improve draft set could not be repaired" in body
    assert "file.py missing" in body
    assert "syntax error" in body
    assert "file.py" in body
    assert "## Environment" not in body


def test_agent_credential_failure_body_contains_expected_sections():
    from pycastle.upstream_issue_report import agent_credential_failure_body

    body = agent_credential_failure_body(
        service_name="codex",
        role_name="implement",
        status_code=401,
        raw_result_envelope="{}",
        remediation="Run codex login.",
        observations=(("stderr", "auth failed"),),
    )

    assert "Operator-actionable agent credential failure" in body
    assert "Run codex login." in body
    assert "Service: codex" in body
    assert "Agent: implement" in body
    assert "Status: 401" in body
    assert "auth failed" in body
    assert "## Environment" not in body


def test_diagnostic_mount_fallback_body_contains_expected_sections(tmp_path):
    from pathlib import Path

    from pycastle.managed_worktree_mount_policy import ManagedWorktreeMountRejected
    from pycastle.upstream_issue_report import diagnostic_mount_fallback_body

    rejection = ManagedWorktreeMountRejected(
        caller="diagnose-agent",
        role="implement",
        repo_root=tmp_path,
        mount_path=Path("/bad/path"),
        expected_worktrees_dir=Path("/worktrees"),
        expected_mount_path=Path("/worktrees/feat"),
        rejection_code="invalid_mount_path",
        invariant="mount must be inside worktrees dir",
        detail="Expected parent /worktrees, got /bad.",
        actual_parent=Path("/bad"),
    )

    body = diagnostic_mount_fallback_body(
        caller="diagnose-agent",
        diagnostic_role="diagnoser",
        role_name="implement",
        original_failure_summary="test failed",
        rejection=rejection,
    )

    assert "## Diagnostic fallback" in body
    assert "diagnose-agent" in body
    assert "implement" in body
    assert "diagnoser" in body
    assert "/worktrees/feat" in body
    assert "/bad/path" in body
    assert "invalid_mount_path" in body
    assert "test failed" in body
    assert "## Environment" not in body
