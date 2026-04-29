from unittest.mock import MagicMock

from pycastle.git_service import GitCommandError, GitService, GitTimeoutError
from pycastle.labels import (
    LABEL_BUG,
    LABEL_NEEDS_INFO,
    LABEL_NEEDS_TRIAGE,
    LABEL_READY_FOR_AGENT,
    LABEL_READY_FOR_HUMAN,
    LABEL_WONTFIX,
    LABELS,
    _get_remote_repo,
)


# ── Cycle 2: Label name constants ─────────────────────────────────────────────


def test_label_constants_exist():
    assert LABEL_BUG == "bug"
    assert LABEL_NEEDS_INFO == "needs-info"
    assert LABEL_NEEDS_TRIAGE == "needs-triage"
    assert LABEL_READY_FOR_AGENT == "ready-for-agent"
    assert LABEL_READY_FOR_HUMAN == "ready-for-human"
    assert LABEL_WONTFIX == "wontfix"


def test_labels_list_uses_constants():
    names = {entry["name"] for entry in LABELS}
    assert LABEL_BUG in names
    assert LABEL_NEEDS_INFO in names
    assert LABEL_NEEDS_TRIAGE in names
    assert LABEL_READY_FOR_AGENT in names
    assert LABEL_READY_FOR_HUMAN in names
    assert LABEL_WONTFIX in names


# ── Cycle 1: _get_remote_repo with injected GitService ───────────────────────


def test_get_remote_repo_returns_owner_and_repo_from_https_url():
    mock_svc = MagicMock(spec=GitService)
    mock_svc.get_remote_url.return_value = "https://github.com/owner/repo.git"
    result = _get_remote_repo(git_service=mock_svc)
    assert result == ("owner", "repo")


def test_get_remote_repo_returns_owner_and_repo_from_ssh_url():
    mock_svc = MagicMock(spec=GitService)
    mock_svc.get_remote_url.return_value = "git@github.com:owner/repo.git"
    result = _get_remote_repo(git_service=mock_svc)
    assert result == ("owner", "repo")


def test_get_remote_repo_returns_none_for_non_github_url():
    mock_svc = MagicMock(spec=GitService)
    mock_svc.get_remote_url.return_value = "https://gitlab.com/owner/repo.git"
    result = _get_remote_repo(git_service=mock_svc)
    assert result is None


def test_get_remote_repo_returns_none_on_git_command_error():
    mock_svc = MagicMock(spec=GitService)
    mock_svc.get_remote_url.side_effect = GitCommandError("no remote", returncode=2)
    result = _get_remote_repo(git_service=mock_svc)
    assert result is None


def test_get_remote_repo_returns_none_on_timeout():
    mock_svc = MagicMock(spec=GitService)
    mock_svc.get_remote_url.side_effect = GitTimeoutError("timed out")
    result = _get_remote_repo(git_service=mock_svc)
    assert result is None


def test_get_remote_repo_strips_dot_git_suffix():
    mock_svc = MagicMock(spec=GitService)
    mock_svc.get_remote_url.return_value = "https://github.com/owner/repo.git"
    owner, repo = _get_remote_repo(git_service=mock_svc)
    assert repo == "repo"
