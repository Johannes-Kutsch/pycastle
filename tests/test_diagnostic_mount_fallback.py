from unittest.mock import MagicMock

import pytest

from pycastle.diagnostic_mount_fallback import (
    DiagnosticMountFallbackIssue,
    decide_diagnostic_mount_dispatch,
)
from pycastle.managed_worktree_mount_policy import ManagedWorktreeMountAccepted
from pycastle.services.github_service import GithubServiceError


def _rejecting_mount_setup(tmp_path):
    (tmp_path / "pycastle" / ".worktrees").mkdir(parents=True, exist_ok=True)
    invalid_mount = tmp_path / "outside-worktrees" / "preflight-sandbox"
    invalid_mount.mkdir(parents=True, exist_ok=True)
    return invalid_mount


def test_decide_diagnostic_mount_dispatch_files_issue_and_returns_number(tmp_path):
    invalid_mount = _rejecting_mount_setup(tmp_path)
    github_svc = MagicMock()
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []
    github_svc.create_issue_in.return_value = (99, 10099)

    result = decide_diagnostic_mount_dispatch(
        repo_root=tmp_path,
        mount_path=invalid_mount,
        caller="Pre-Flight Reporter",
        diagnostic_role="preflight_issue",
        role_name="preflight_issue",
        original_failure_summary="Preflight check 'ruff' failed.",
        github_svc=github_svc,
    )

    assert isinstance(result, DiagnosticMountFallbackIssue)
    assert result.issue_number == 99
    assert (
        "[pycastle] Pre-Flight Reporter skipped for role preflight_issue:"
        in result.title
    )
    github_svc.create_issue_in.assert_called_once()


def test_decide_diagnostic_mount_dispatch_raises_on_create_failure(tmp_path):
    from pycastle.services import GithubNetworkError

    invalid_mount = _rejecting_mount_setup(tmp_path)
    github_svc = MagicMock()
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []
    github_svc.create_issue_in.side_effect = GithubNetworkError(
        "create failed", cause=OSError("refused")
    )

    with pytest.raises(GithubServiceError):
        decide_diagnostic_mount_dispatch(
            repo_root=tmp_path,
            mount_path=invalid_mount,
            caller="Pre-Flight Reporter",
            diagnostic_role="preflight_issue",
            role_name="preflight_issue",
            original_failure_summary="Preflight check 'ruff' failed.",
            github_svc=github_svc,
        )


def test_decide_diagnostic_mount_dispatch_preserves_non_rejecting_mount_path(
    tmp_path,
):
    worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    missing_mount = worktrees_dir / "preflight-sandbox"
    github_svc = MagicMock()

    result = decide_diagnostic_mount_dispatch(
        repo_root=tmp_path,
        mount_path=missing_mount,
        caller="Pre-Flight Reporter",
        diagnostic_role="preflight_issue",
        role_name="preflight_issue",
        original_failure_summary="Preflight check 'ruff' failed.",
        github_svc=github_svc,
    )

    assert result == ManagedWorktreeMountAccepted(
        caller="Pre-Flight Reporter",
        role="preflight_issue",
        repo_root=tmp_path,
        mount_path=missing_mount,
        expected_worktrees_dir=worktrees_dir,
    )
    github_svc.search_open_issues_by_title.assert_not_called()
    github_svc.create_issue_in.assert_not_called()
