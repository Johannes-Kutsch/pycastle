import pytest
from unittest.mock import MagicMock, patch

from pycastle.config import Config
from pycastle.services import GitCommandError, GitService, GitTimeoutError
from pycastle.labels import _get_remote_repo, create_labels_interactive


# ── Shared fixture ─────────────────────────────────────────────────────────────


@pytest.fixture
def label_setup(monkeypatch):
    """Provide a known GitHub remote and capture POST payloads without prompts."""
    mock_svc = MagicMock(spec=GitService)
    mock_svc.get_remote_url.return_value = "https://github.com/owner/repo.git"

    # "Target repo owner/repo?" → True; "Delete all existing labels first?" → False
    monkeypatch.setattr(
        "pycastle.labels.click.confirm",
        lambda msg, **kw: "Target repo" in msg,
    )

    posted: list = []

    def fake_gh(method, path, token, data=None):
        if method == "POST":
            posted.append(data)
        return (201, None)

    return mock_svc, posted, fake_gh


# ── Labels built from config ───────────────────────────────────────────────────


def test_create_labels_interactive_posts_exactly_three_labels(label_setup):
    mock_svc, posted, fake_gh = label_setup
    with patch("pycastle.labels._gh", fake_gh):
        create_labels_interactive("tok", git_service=mock_svc, cfg=Config())
    assert len(posted) == 3


def test_create_labels_interactive_posts_bug_issue_and_hitl_names(label_setup):
    mock_svc, posted, fake_gh = label_setup
    cfg = Config()
    with patch("pycastle.labels._gh", fake_gh):
        create_labels_interactive("tok", git_service=mock_svc, cfg=cfg)
    names = {entry["name"] for entry in posted}
    assert cfg.bug_label in names
    assert cfg.issue_label in names
    assert cfg.hitl_label in names


def test_create_labels_interactive_does_not_post_removed_label_names(label_setup):
    mock_svc, posted, fake_gh = label_setup
    with patch("pycastle.labels._gh", fake_gh):
        create_labels_interactive("tok", git_service=mock_svc, cfg=Config())
    names = {entry["name"] for entry in posted}
    assert "needs-info" not in names
    assert "needs-triage" not in names
    assert "wontfix" not in names


def test_create_labels_interactive_posts_entries_with_required_github_api_keys(
    label_setup,
):
    mock_svc, posted, fake_gh = label_setup
    with patch("pycastle.labels._gh", fake_gh):
        create_labels_interactive("tok", git_service=mock_svc, cfg=Config())
    for entry in posted:
        assert "name" in entry
        assert "description" in entry
        assert "color" in entry


def test_create_labels_interactive_returns_early_when_repo_not_resolved(monkeypatch):
    mock_svc = MagicMock(spec=GitService)
    mock_svc.get_remote_url.return_value = "https://gitlab.com/owner/repo.git"
    # Non-GitHub URL → _get_remote_repo returns None; click.prompt for manual slug
    monkeypatch.setattr("pycastle.labels.click.prompt", lambda *a, **kw: "invalid")
    posted: list = []
    with patch("pycastle.labels._gh", lambda *a, **kw: (201, None)):
        create_labels_interactive("tok", git_service=mock_svc, cfg=Config())
    assert posted == []


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


def test_main_reads_gh_token_from_config_env_file(monkeypatch, tmp_path):
    import pycastle.labels as labels_mod

    monkeypatch.chdir(tmp_path)
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / ".env").write_text("GH_TOKEN=from-env-file\n")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("PYCASTLE_HOME", raising=False)

    received: list[str] = []
    monkeypatch.setattr(
        labels_mod,
        "create_labels_interactive",
        lambda token, cfg=None: received.append(token),
    )

    labels_mod.main(cfg=Config())

    assert received == ["from-env-file"]
