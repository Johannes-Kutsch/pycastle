from unittest.mock import MagicMock, patch

from pycastle.config import Config
from pycastle.git_service import GitCommandError, GitService, GitTimeoutError
from pycastle.labels import _get_remote_repo, create_labels_interactive


# ── Labels built from config ───────────────────────────────────────────────────


def test_create_labels_interactive_posts_exactly_three_labels(monkeypatch):
    monkeypatch.setattr(
        "pycastle.labels._resolve_repo", lambda token, *a, **kw: ("owner", "repo")
    )
    monkeypatch.setattr("pycastle.labels.click.confirm", lambda *a, **kw: False)
    posted: list = []

    def fake_gh(method, path, token, data=None):
        if method == "POST":
            posted.append(data)
        return (201, None)

    with patch("pycastle.labels._gh", fake_gh):
        create_labels_interactive("tok", cfg=Config())

    assert len(posted) == 3


def test_create_labels_interactive_posts_bug_issue_and_hitl_names(monkeypatch):
    monkeypatch.setattr(
        "pycastle.labels._resolve_repo", lambda token, *a, **kw: ("owner", "repo")
    )
    monkeypatch.setattr("pycastle.labels.click.confirm", lambda *a, **kw: False)
    posted: list = []

    def fake_gh(method, path, token, data=None):
        if method == "POST":
            posted.append(data)
        return (201, None)

    cfg = Config()
    with patch("pycastle.labels._gh", fake_gh):
        create_labels_interactive("tok", cfg=cfg)

    names = {entry["name"] for entry in posted}
    assert cfg.bug_label in names
    assert cfg.issue_label in names
    assert cfg.hitl_label in names


def test_create_labels_interactive_does_not_post_removed_label_names(monkeypatch):
    monkeypatch.setattr(
        "pycastle.labels._resolve_repo", lambda token, *a, **kw: ("owner", "repo")
    )
    monkeypatch.setattr("pycastle.labels.click.confirm", lambda *a, **kw: False)
    posted: list = []

    def fake_gh(method, path, token, data=None):
        if method == "POST":
            posted.append(data)
        return (201, None)

    with patch("pycastle.labels._gh", fake_gh):
        create_labels_interactive("tok", cfg=Config())

    names = {entry["name"] for entry in posted}
    assert "needs-info" not in names
    assert "needs-triage" not in names
    assert "wontfix" not in names


def test_create_labels_interactive_posts_entries_with_required_github_api_keys(
    monkeypatch,
):
    monkeypatch.setattr(
        "pycastle.labels._resolve_repo", lambda token, *a, **kw: ("owner", "repo")
    )
    monkeypatch.setattr("pycastle.labels.click.confirm", lambda *a, **kw: False)
    posted: list = []

    def fake_gh(method, path, token, data=None):
        if method == "POST":
            posted.append(data)
        return (201, None)

    with patch("pycastle.labels._gh", fake_gh):
        create_labels_interactive("tok", cfg=Config())

    for entry in posted:
        assert "name" in entry
        assert "description" in entry
        assert "color" in entry


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

    loaded_paths: list = []
    monkeypatch.setattr(dotenv, "load_dotenv", lambda path: loaded_paths.append(path))
    monkeypatch.setenv("GH_TOKEN", "fake-token")
    monkeypatch.setattr(
        labels_mod, "create_labels_interactive", lambda token, cfg=None: None
    )

    labels_mod.main()

    assert loaded_paths == [Config().env_file]
