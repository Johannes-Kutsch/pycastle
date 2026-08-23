from unittest.mock import MagicMock

from pycastle.diagnostic_mount_fallback import (
    decide_diagnostic_mount_dispatch,
)
from pycastle.managed_worktree_mount_policy import ManagedWorktreeMountAccepted


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
