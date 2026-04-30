from unittest.mock import MagicMock

from pycastle.git_service import GitCommandError, GitService, GitTimeoutError
from pycastle.labels import LABELS, _get_remote_repo


# ── LABELS list ────────────────────────────────────────────────────────────────


def test_labels_contains_exactly_three_entries():
    assert len(LABELS) == 3


def test_labels_contains_bug_issue_and_hitl():
    names = {entry["name"] for entry in LABELS}
    assert "bug" in names
    assert "ready-for-agent" in names
    assert "ready-for-human" in names


def test_labels_does_not_contain_removed_labels():
    names = {entry["name"] for entry in LABELS}
    assert "needs-info" not in names
    assert "needs-triage" not in names
    assert "wontfix" not in names


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


# ── Issue 269: labels.main() uses config.env_file ────────────────────────────


def test_main_loads_dotenv_from_config_env_file(monkeypatch):
    import dotenv

    import pycastle.labels as labels_mod
    from pycastle.config import config as cfg

    loaded_paths: list = []
    monkeypatch.setattr(dotenv, "load_dotenv", lambda path: loaded_paths.append(path))
    monkeypatch.setenv("GH_TOKEN", "fake-token")
    monkeypatch.setattr(labels_mod, "create_labels_interactive", lambda token: None)

    labels_mod.main()

    assert loaded_paths == [cfg.env_file]
