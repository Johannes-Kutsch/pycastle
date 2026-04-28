from unittest.mock import MagicMock


from pycastle.git_service import GitCommandError, GitService, GitTimeoutError
from pycastle.labels import _get_remote_repo


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


def test_get_remote_repo_calls_get_remote_url_with_origin():
    mock_svc = MagicMock(spec=GitService)
    mock_svc.get_remote_url.return_value = "https://github.com/owner/repo.git"
    _get_remote_repo(git_service=mock_svc)
    mock_svc.get_remote_url.assert_called_once_with("origin")
