import pytest
from unittest.mock import MagicMock

from pycastle.config import Config
from pycastle.services import (
    GithubAPIError,
    GithubService,
    GitService,
    OperatorActionableGithubError,
)
from pycastle.commands.labels import _resolve_repo, create_labels_interactive


# ── Shared fixture ─────────────────────────────────────────────────────────────


@pytest.fixture
def label_setup(monkeypatch):
    """Provide a known GitHub remote and capture create_label payloads without prompts."""
    git_svc = MagicMock(spec=GitService)
    git_svc.get_github_remote_repo.return_value = ("owner", "repo")

    # "Target repo owner/repo?" → True; "Delete all existing labels first?" → False
    monkeypatch.setattr(
        "pycastle.commands.labels.click.confirm",
        lambda msg, **kw: "Target repo" in msg,
    )

    posted: list = []
    github_svc = MagicMock(spec=GithubService)
    github_svc.create_label.side_effect = lambda body: posted.append(body)

    return git_svc, github_svc, posted


# ── Labels built from config ───────────────────────────────────────────────────


def test_create_labels_interactive_posts_exactly_eleven_labels(label_setup):
    git_svc, github_svc, posted = label_setup
    create_labels_interactive(
        "tok", git_service=git_svc, cfg=Config(), github_service=github_svc
    )
    assert len(posted) == 11


def test_create_labels_interactive_posts_all_canonical_label_names(label_setup):
    git_svc, github_svc, posted = label_setup
    cfg = Config()
    create_labels_interactive(
        "tok", git_service=git_svc, cfg=cfg, github_service=github_svc
    )
    names = {entry["name"] for entry in posted}
    assert cfg.bug_label in names
    assert cfg.enhancement_label in names
    assert cfg.needs_triage_label in names
    assert cfg.needs_info_label in names
    assert cfg.issue_label in names
    assert cfg.hitl_label in names
    assert cfg.wontfix_label in names
    assert cfg.refactor_slice_label in names
    assert cfg.behavior_slice_label in names
    assert cfg.docs_slice_label in names
    assert cfg.needs_slice_type_label in names


def test_needs_slice_type_label_has_correct_color_and_description(label_setup):
    git_svc, github_svc, posted = label_setup
    cfg = Config()
    create_labels_interactive(
        "tok", git_service=git_svc, cfg=cfg, github_service=github_svc
    )
    entry = next(e for e in posted if e["name"] == cfg.needs_slice_type_label)
    assert entry["color"] == "d73a4a"
    assert (
        entry["description"]
        == "ready-for-agent issue missing exactly one slice-mode label"
    )


def test_create_labels_interactive_preserves_all_canonical_label_metadata(label_setup):
    git_svc, github_svc, posted = label_setup
    cfg = Config()
    create_labels_interactive(
        "tok", git_service=git_svc, cfg=cfg, github_service=github_svc
    )
    entries_by_name = {entry["name"]: entry for entry in posted}

    assert entries_by_name == {
        cfg.bug_label: {
            "name": cfg.bug_label,
            "description": "Something isn't working",
            "color": "d73a4a",
        },
        cfg.issue_label: {
            "name": cfg.issue_label,
            "description": "Fully specified, ready for afk agent",
            "color": "0be348",
        },
        cfg.hitl_label: {
            "name": cfg.hitl_label,
            "description": "Requires human implementation",
            "color": "5181b8",
        },
        cfg.enhancement_label: {
            "name": cfg.enhancement_label,
            "description": "New feature or request",
            "color": "a2eeef",
        },
        cfg.needs_triage_label: {
            "name": cfg.needs_triage_label,
            "description": "Maintainer needs to evaluate this issue",
            "color": "fbca04",
        },
        cfg.needs_info_label: {
            "name": cfg.needs_info_label,
            "description": "Waiting on reporter for more information",
            "color": "b03176",
        },
        cfg.wontfix_label: {
            "name": cfg.wontfix_label,
            "description": "Will not be actioned",
            "color": "ffffff",
        },
        cfg.refactor_slice_label: {
            "name": cfg.refactor_slice_label,
            "description": "Implementation scope: structural refactor only",
            "color": "0be348",
        },
        cfg.behavior_slice_label: {
            "name": cfg.behavior_slice_label,
            "description": "Implementation scope: observable behavior change",
            "color": "0be348",
        },
        cfg.docs_slice_label: {
            "name": cfg.docs_slice_label,
            "description": "Implementation scope: documentation only",
            "color": "0be348",
        },
        cfg.needs_slice_type_label: {
            "name": cfg.needs_slice_type_label,
            "description": "ready-for-agent issue missing exactly one slice-mode label",
            "color": "d73a4a",
        },
    }


def test_create_labels_interactive_posts_entries_with_required_github_api_keys(
    label_setup,
):
    git_svc, github_svc, posted = label_setup
    create_labels_interactive(
        "tok", git_service=git_svc, cfg=Config(), github_service=github_svc
    )
    for entry in posted:
        assert "name" in entry
        assert "description" in entry
        assert "color" in entry


def test_create_labels_interactive_calls_check_auth(label_setup):
    git_svc, github_svc, _ = label_setup
    create_labels_interactive(
        "tok", git_service=git_svc, cfg=Config(), github_service=github_svc
    )
    github_svc.check_auth.assert_called_once()


def test_create_labels_interactive_exits_on_github_retry_exhaustion(monkeypatch):
    git_svc = MagicMock(spec=GitService)
    git_svc.get_github_remote_repo.return_value = ("owner", "repo")
    monkeypatch.setattr(
        "pycastle.commands.labels.click.confirm",
        lambda msg, **kw: "Target repo" in msg,
    )
    messages: list[str] = []
    monkeypatch.setattr(
        "pycastle.commands.labels.click.echo",
        lambda message, err=False: messages.append(str(message)),
    )

    github_svc = MagicMock(spec=GithubService)
    github_svc.check_auth.side_effect = OperatorActionableGithubError(
        "GitHub API GET /user failed after 4 attempts: GitHub API GET /user returned 502: Bad Gateway",
        method="GET",
        path="/user",
        attempt_count=4,
        cause=GithubAPIError(
            "GitHub API GET /user returned 502: Bad Gateway",
            status=502,
            body="Bad Gateway",
            method="GET",
            path="/user",
        ),
    )

    with pytest.raises(SystemExit) as exc_info:
        create_labels_interactive(
            "tok", git_service=git_svc, cfg=Config(), github_service=github_svc
        )

    assert exc_info.value.code == 1
    assert any(
        "GitHub request retry limit reached:" in message
        and "failed after 4 attempts" in message
        for message in messages
    )


def test_create_labels_interactive_exits_on_label_creation_retry_exhaustion(
    monkeypatch,
):
    git_svc = MagicMock(spec=GitService)
    git_svc.get_github_remote_repo.return_value = ("owner", "repo")
    monkeypatch.setattr(
        "pycastle.commands.labels.click.confirm",
        lambda msg, **kw: "Target repo" in msg,
    )
    messages: list[str] = []
    monkeypatch.setattr(
        "pycastle.commands.labels.click.echo",
        lambda message, err=False: messages.append(str(message)),
    )

    github_svc = MagicMock(spec=GithubService)
    github_svc.create_label.side_effect = OperatorActionableGithubError(
        "GitHub API POST /repos/owner/repo/labels failed after 4 attempts: "
        "GitHub API POST /repos/owner/repo/labels returned 502: Bad Gateway",
        method="POST",
        path="/repos/owner/repo/labels",
        attempt_count=4,
        cause=GithubAPIError(
            "GitHub API POST /repos/owner/repo/labels returned 502: Bad Gateway",
            status=502,
            body="Bad Gateway",
            method="POST",
            path="/repos/owner/repo/labels",
        ),
    )

    with pytest.raises(SystemExit) as exc_info:
        create_labels_interactive(
            "tok", git_service=git_svc, cfg=Config(), github_service=github_svc
        )

    assert exc_info.value.code == 1
    assert any(
        "GitHub request retry limit reached:" in message
        and "POST /repos/owner/repo/labels" in message
        for message in messages
    )


def test_create_labels_interactive_returns_early_when_repo_not_resolved(monkeypatch):
    git_svc = MagicMock(spec=GitService)
    git_svc.get_github_remote_repo.return_value = None
    monkeypatch.setattr(
        "pycastle.commands.labels.click.prompt", lambda *a, **kw: "invalid"
    )
    github_svc = MagicMock(spec=GithubService)
    create_labels_interactive(
        "tok", git_service=git_svc, cfg=Config(), github_service=github_svc
    )
    github_svc.create_label.assert_not_called()


def test_create_labels_interactive_skips_label_on_422(monkeypatch):
    git_svc = MagicMock(spec=GitService)
    git_svc.get_github_remote_repo.return_value = ("owner", "repo")
    monkeypatch.setattr(
        "pycastle.commands.labels.click.confirm",
        lambda msg, **kw: "Target repo" in msg,
    )
    github_svc = MagicMock(spec=GithubService)
    github_svc.create_label.side_effect = GithubAPIError(
        "exists", status=422, body="", method="POST", path="/labels"
    )
    # should not raise
    create_labels_interactive(
        "tok", git_service=git_svc, cfg=Config(), github_service=github_svc
    )


def test_create_labels_interactive_resets_existing_labels_when_confirmed(monkeypatch):
    git_svc = MagicMock(spec=GitService)
    git_svc.get_github_remote_repo.return_value = ("owner", "repo")
    # both confirm prompts → True
    monkeypatch.setattr(
        "pycastle.commands.labels.click.confirm", lambda msg, **kw: True
    )

    github_svc = MagicMock(spec=GithubService)
    github_svc.list_labels.return_value = [{"name": "old"}, {"name": "ancient"}]
    create_labels_interactive(
        "tok", git_service=git_svc, cfg=Config(), github_service=github_svc
    )
    deleted = [c.args[0] for c in github_svc.delete_label.call_args_list]
    assert deleted == ["old", "ancient"]


# ── _resolve_repo ──────────────────────────────────────────────────────────────


def test_resolve_repo_uses_git_service_for_auto_detect(monkeypatch):
    git_svc = MagicMock(spec=GitService)
    git_svc.get_github_remote_repo.return_value = ("owner", "repo")
    monkeypatch.setattr(
        "pycastle.commands.labels.click.confirm", lambda msg, **kw: True
    )
    assert _resolve_repo(git_service=git_svc, cfg=Config()) == ("owner", "repo")


def test_resolve_repo_falls_back_to_slug_when_no_remote(monkeypatch):
    git_svc = MagicMock(spec=GitService)
    git_svc.get_github_remote_repo.return_value = None
    monkeypatch.setattr("pycastle.commands.labels.click.prompt", lambda *a, **kw: "x/y")
    assert _resolve_repo(git_service=git_svc, cfg=Config()) == ("x", "y")


def test_resolve_repo_returns_none_for_invalid_slug(monkeypatch):
    git_svc = MagicMock(spec=GitService)
    git_svc.get_github_remote_repo.return_value = None
    monkeypatch.setattr(
        "pycastle.commands.labels.click.prompt", lambda *a, **kw: "invalid"
    )
    assert _resolve_repo(git_service=git_svc, cfg=Config()) is None


# ── Issue 269: labels.main() uses config.env_file ────────────────────────────


def test_main_reads_gh_token_from_config_env_file(monkeypatch, tmp_path):
    import pycastle.commands.labels as labels_mod

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


def test_main_reads_gh_token_from_global_env_layer_when_local_absent(
    monkeypatch, tmp_path
):
    import pycastle.commands.labels as labels_mod

    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / ".env").write_text("GH_TOKEN=from-global-env\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("PYCASTLE_HOME", str(global_dir))

    received: list[str] = []
    monkeypatch.setattr(
        labels_mod,
        "create_labels_interactive",
        lambda token, cfg=None: received.append(token),
    )

    labels_mod.main(cfg=Config())

    assert received == ["from-global-env"]
