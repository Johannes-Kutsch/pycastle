import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pycastle.config import Config
from pycastle.services import (
    GitCommandError,
    GitNotFoundError,
    GitService,
    GitServiceError,
    GitTimeoutError,
)

_cfg = Config()


# ── Exception hierarchy ────────────────────────────────────────────────────────


def test_git_service_error_is_runtime_error():
    assert issubclass(GitServiceError, RuntimeError)


def test_git_command_error_is_git_service_error():
    assert issubclass(GitCommandError, GitServiceError)


def test_git_timeout_error_is_git_service_error_and_timeout_error():
    assert issubclass(GitTimeoutError, GitServiceError)
    assert issubclass(GitTimeoutError, TimeoutError)


def test_git_not_found_error_is_git_service_error():
    assert issubclass(GitNotFoundError, GitServiceError)


def test_git_command_error_carries_returncode_and_stderr():
    err = GitCommandError("msg", returncode=128, stderr="fatal: bad ref")
    assert err.returncode == 128
    assert err.stderr == "fatal: bad ref"


# ── Config injection ───────────────────────────────────────────────────────────


def test_git_service_uses_worktree_timeout_from_injected_config():
    svc = GitService(cfg=Config(worktree_timeout=1))
    assert svc.timeout == 1


# ── get_user_name() ────────────────────────────────────────────────────────────


def test_get_user_name_returns_name():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"Alice\n", stderr=b""),
    ):
        assert svc.get_user_name() == "Alice"


def test_get_user_name_raises_git_command_error_on_failure():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=1, stdout=b"", stderr=b"error"),
    ):
        with pytest.raises(GitCommandError):
            svc.get_user_name()


def test_get_user_name_raises_git_timeout_error_on_timeout():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.get_user_name()


def test_get_user_name_raises_git_not_found_error_when_git_missing():
    svc = GitService(_cfg)
    with patch("subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(GitNotFoundError):
            svc.get_user_name()


def test_get_user_name_strips_trailing_newline():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"  Bob  \n", stderr=b""),
    ):
        assert svc.get_user_name() == "Bob"


# ── get_branch_commit_subjects() ──────────────────────────────────────────────


def test_get_branch_commit_subjects_returns_subjects_most_recent_first():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(
            returncode=0,
            stdout=b"RALPH: Review - fix auth\nRALPH: implement auth\n",
            stderr=b"",
        ),
    ):
        result = svc.get_branch_commit_subjects("pycastle/issue-1", Path("/repo"))
    assert result == ["RALPH: Review - fix auth", "RALPH: implement auth"]


def test_get_branch_commit_subjects_returns_empty_list_when_no_commits_ahead():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"", stderr=b""),
    ):
        result = svc.get_branch_commit_subjects("pycastle/issue-1", Path("/repo"))
    assert result == []


def test_get_branch_commit_subjects_returns_empty_list_when_branch_missing():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=128, stdout=b"", stderr=b"unknown revision"),
    ):
        result = svc.get_branch_commit_subjects("pycastle/issue-99", Path("/repo"))
    assert result == []


def test_get_branch_commit_subjects_raises_git_timeout_error_on_timeout():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.get_branch_commit_subjects("pycastle/issue-1", Path("/repo"))


# ── get_user_email() ───────────────────────────────────────────────────────────


def test_get_user_email_returns_email():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"alice@example.com\n", stderr=b""),
    ):
        assert svc.get_user_email() == "alice@example.com"


def test_get_user_email_raises_git_command_error_on_failure():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=1, stdout=b"", stderr=b"error"),
    ):
        with pytest.raises(GitCommandError):
            svc.get_user_email()


def test_get_user_email_raises_git_timeout_error_on_timeout():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.get_user_email()


# ── is_ancestor() ──────────────────────────────────────────────────────────────


def test_is_ancestor_returns_true_when_ancestor():
    svc = GitService(_cfg)
    with patch("subprocess.run", return_value=MagicMock(returncode=0)):
        assert svc.is_ancestor("main", Path("/tmp/repo")) is True


def test_is_ancestor_returns_false_when_not_ancestor():
    svc = GitService(_cfg)
    with patch("subprocess.run", return_value=MagicMock(returncode=1)):
        assert svc.is_ancestor("feature/x", Path("/tmp/repo")) is False


def test_is_ancestor_raises_git_timeout_error_on_timeout():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.is_ancestor("main", Path("/tmp/repo"))


# ── verify_ref_exists() ────────────────────────────────────────────────────────


def test_verify_ref_exists_returns_true_when_ref_present():
    svc = GitService(_cfg)
    with patch("subprocess.run", return_value=MagicMock(returncode=0)):
        assert svc.verify_ref_exists("main", Path("/tmp/repo")) is True


def test_verify_ref_exists_returns_false_when_ref_absent():
    svc = GitService(_cfg)
    with patch("subprocess.run", return_value=MagicMock(returncode=1)):
        assert svc.verify_ref_exists("nonexistent", Path("/tmp/repo")) is False


def test_verify_ref_exists_raises_git_timeout_error_on_timeout():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.verify_ref_exists("main", Path("/tmp/repo"))


# ── delete_branch() ───────────────────────────────────────────────────────────


def test_delete_branch_succeeds_silently():
    svc = GitService(_cfg)
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr=b"")):
        svc.delete_branch("feature/old", Path("/tmp/repo"))  # must not raise


def test_delete_branch_raises_git_command_error_on_failure():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=1, stderr=b"error: branch not found"),
    ):
        with pytest.raises(GitCommandError):
            svc.delete_branch("nonexistent", Path("/tmp/repo"))


def test_delete_branch_raises_git_timeout_error_on_timeout():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.delete_branch("main", Path("/tmp/repo"))


# ── list_worktrees() ──────────────────────────────────────────────────────────


_WORKTREE_PORCELAIN = (
    b"worktree /home/user/repo\n"
    b"HEAD abc123\n"
    b"branch refs/heads/main\n"
    b"\n"
    b"worktree /home/user/repo/worktrees/feature-x\n"
    b"HEAD def456\n"
    b"branch refs/heads/feature/x\n"
    b"\n"
)


def test_list_worktrees_returns_list_of_paths():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=_WORKTREE_PORCELAIN),
    ):
        result = svc.list_worktrees(Path("/tmp/repo"))
    assert result == [
        Path("/home/user/repo"),
        Path("/home/user/repo/worktrees/feature-x"),
    ]


def test_list_worktrees_returns_empty_list_for_no_output():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b""),
    ):
        assert svc.list_worktrees(Path("/tmp/repo")) == []


def test_list_worktrees_raises_git_command_error_on_failure():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=1, stdout=b"", stderr=b"error"),
    ):
        with pytest.raises(GitCommandError):
            svc.list_worktrees(Path("/tmp/repo"))


def test_list_worktrees_raises_git_timeout_error_on_timeout():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.list_worktrees(Path("/tmp/repo"))


# ── get_remote_url() ──────────────────────────────────────────────────────────


def test_get_remote_url_returns_url():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(
            returncode=0,
            stdout=b"https://github.com/owner/repo.git\n",
        ),
    ):
        assert svc.get_remote_url() == "https://github.com/owner/repo.git"


def test_get_remote_url_raises_git_command_error_on_failure():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=2, stdout=b"", stderr=b"no such remote"),
    ):
        with pytest.raises(GitCommandError):
            svc.get_remote_url()


def test_get_remote_url_raises_git_timeout_error_on_timeout():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.get_remote_url()


def test_get_remote_url_uses_custom_remote():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(
            returncode=0, stdout=b"https://github.com/owner/repo.git\n"
        ),
    ) as m:
        svc.get_remote_url(remote="upstream")
    cmd = m.call_args[0][0]
    assert "upstream" in cmd


# ── create_worktree() ─────────────────────────────────────────────────────────


def _make_fake_run(prune_rc=0, rev_parse_rc=1, add_rc=0):
    def fake_run(cmd, **kwargs):
        if "prune" in cmd:
            return MagicMock(returncode=prune_rc, stdout=b"", stderr=b"")
        if "rev-parse" in cmd:
            return MagicMock(returncode=rev_parse_rc, stdout=b"abc\n", stderr=b"")
        if "add" in cmd:
            return MagicMock(returncode=add_rc, stdout=b"", stderr=b"fatal: conflict")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    return fake_run


def test_create_worktree_creates_new_branch_when_ref_missing(tmp_path):
    svc = GitService(_cfg)
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        if "prune" in cmd:
            return MagicMock(returncode=0, stdout=b"", stderr=b"")
        if "rev-parse" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        svc.create_worktree(tmp_path, tmp_path / "wt", "feature/new")

    add_cmd = next(c for c in captured if "add" in c and "worktree" in c)
    assert "-b" in add_cmd


def test_create_worktree_uses_existing_branch_when_ref_exists(tmp_path):
    svc = GitService(_cfg)
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        if "prune" in cmd:
            return MagicMock(returncode=0, stdout=b"", stderr=b"")
        if "rev-parse" in cmd:
            return MagicMock(returncode=0, stdout=b"abc\n", stderr=b"")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        svc.create_worktree(tmp_path, tmp_path / "wt", "existing-branch")

    add_cmd = next(c for c in captured if "add" in c and "worktree" in c)
    assert "-b" not in add_cmd


def test_create_worktree_raises_git_command_error_on_add_failure(tmp_path):
    svc = GitService(_cfg)
    with patch("subprocess.run", side_effect=_make_fake_run(add_rc=1)):
        with pytest.raises(GitCommandError):
            svc.create_worktree(tmp_path, tmp_path / "wt", "feature/conflict")


def test_create_worktree_raises_git_timeout_error_on_timeout(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.create_worktree(tmp_path, tmp_path / "wt", "feature/new")


def test_create_worktree_raises_git_command_error_when_remove_fails(tmp_path):
    svc = GitService(_cfg)
    worktree = tmp_path / "wt"
    worktree.mkdir()

    def fake_run(cmd, **kwargs):
        if "remove" in cmd and "worktree" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"not a worktree")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(GitCommandError):
            svc.create_worktree(tmp_path, worktree, "feature/new")


def test_create_worktree_removes_existing_dir_before_add(tmp_path):
    svc = GitService(_cfg)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        svc.create_worktree(tmp_path, worktree, "feature/new")

    cmds_str = [" ".join(c) for c in captured]
    assert any("worktree remove" in c for c in cmds_str)


def test_create_worktree_uses_sha_as_start_point_when_provided(tmp_path):
    svc = GitService(_cfg)
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        if "rev-parse" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        svc.create_worktree(tmp_path, tmp_path / "wt", "feature/new", sha="abc123")

    add_cmd = next(c for c in captured if "add" in c and "worktree" in c)
    assert "abc123" in add_cmd
    assert "HEAD" not in add_cmd


def test_create_worktree_uses_head_when_sha_is_omitted(tmp_path):
    svc = GitService(_cfg)
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        if "rev-parse" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        svc.create_worktree(tmp_path, tmp_path / "wt", "feature/new")

    add_cmd = next(c for c in captured if "add" in c and "worktree" in c)
    assert "HEAD" in add_cmd


def test_create_worktree_uses_head_when_sha_is_none(tmp_path):
    svc = GitService(_cfg)
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        if "rev-parse" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        svc.create_worktree(tmp_path, tmp_path / "wt", "feature/new", sha=None)

    add_cmd = next(c for c in captured if "add" in c and "worktree" in c)
    assert "HEAD" in add_cmd


def test_create_worktree_ignores_sha_when_branch_already_exists(tmp_path):
    svc = GitService(_cfg)
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        if "rev-parse" in cmd:
            return MagicMock(returncode=0, stdout=b"abc\n", stderr=b"")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        svc.create_worktree(tmp_path, tmp_path / "wt", "existing-branch", sha="abc123")

    add_cmd = next(c for c in captured if "add" in c and "worktree" in c)
    assert "-b" not in add_cmd
    assert "abc123" not in add_cmd


# ── remove_worktree() ─────────────────────────────────────────────────────────


def test_remove_worktree_calls_git_worktree_remove(tmp_path):
    svc = GitService(_cfg)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr=b"")) as m:
        svc.remove_worktree(tmp_path, worktree)
    cmd = m.call_args[0][0]
    assert "worktree" in cmd and "remove" in cmd


def test_remove_worktree_falls_back_to_rmtree_when_git_fails(tmp_path):
    svc = GitService(_cfg)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "file.txt").write_text("content")

    with patch("subprocess.run", return_value=MagicMock(returncode=1, stderr=b"error")):
        svc.remove_worktree(tmp_path, worktree)

    assert not worktree.exists()


def test_remove_worktree_raises_git_timeout_error_on_timeout(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.remove_worktree(tmp_path, tmp_path / "wt")


# ── try_merge() ───────────────────────────────────────────────────────────────


def _init_repo(path: Path) -> None:
    """Initialise a bare-minimum git repo with one commit on main."""
    subprocess.run(
        ["git", "init", "-b", "main", str(path)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True
    )


def test_try_merge_returns_true_on_clean_merge(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    # create a branch with a non-conflicting change
    subprocess.run(
        ["git", "checkout", "-b", "feature"], cwd=repo, check=True, capture_output=True
    )
    (repo / "feature.txt").write_text("feature\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "feature commit"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "main"], cwd=repo, check=True, capture_output=True
    )

    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True
    ).stdout.strip()

    svc = GitService(_cfg)
    result = svc.try_merge(repo, "feature")

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True
    ).stdout.strip()

    assert result is True
    assert head_after != head_before


def test_try_merge_returns_false_on_conflict_and_leaves_clean_state(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    # create a branch that edits the same file as main will edit
    subprocess.run(
        ["git", "checkout", "-b", "conflict-branch"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "base.txt").write_text("branch change\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "branch edit"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    # also edit the same file on main so they conflict
    subprocess.run(
        ["git", "checkout", "main"], cwd=repo, check=True, capture_output=True
    )
    (repo / "base.txt").write_text("main change\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "main edit"], cwd=repo, check=True, capture_output=True
    )

    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True
    ).stdout.strip()

    svc = GitService(_cfg)
    result = svc.try_merge(repo, "conflict-branch")

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True
    ).stdout.strip()

    # verify repo is clean (no staged changes, no conflict markers)
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True
    ).stdout.strip()

    assert result is False
    assert head_after == head_before
    assert status == b""


def test_try_merge_raises_git_command_error_on_nonexistent_branch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    svc = GitService(_cfg)
    with pytest.raises(GitCommandError):
        svc.try_merge(repo, "does-not-exist")


def test_try_merge_already_up_to_date_returns_true(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    subprocess.run(
        ["git", "checkout", "-b", "stale"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "checkout", "main"], cwd=repo, check=True, capture_output=True
    )

    svc = GitService(_cfg)
    result = svc.try_merge(repo, "stale")

    assert result is True


def test_try_merge_raises_git_timeout_error_on_timeout(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.try_merge(tmp_path, "feature")


# ── is_working_tree_clean() ───────────────────────────────────────────────────


def test_is_working_tree_clean_returns_true_on_clean_tree(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b""),
    ):
        assert svc.is_working_tree_clean(tmp_path) is True


def test_is_working_tree_clean_returns_false_when_staged_changes_present(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"M  staged_file.py\n"),
    ):
        assert svc.is_working_tree_clean(tmp_path) is False


def test_is_working_tree_clean_returns_false_when_unstaged_tracked_changes_present(
    tmp_path,
):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b" M unstaged_file.py\n"),
    ):
        assert svc.is_working_tree_clean(tmp_path) is False


def test_is_working_tree_clean_returns_true_when_only_untracked_files_present(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"?? new_untracked.py\n"),
    ):
        assert svc.is_working_tree_clean(tmp_path) is True


def test_is_working_tree_clean_ignores_untracked_files_alongside_tracked_changes(
    tmp_path,
):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(
            returncode=0, stdout=b"?? untracked.py\n M tracked.py\n"
        ),
    ):
        assert svc.is_working_tree_clean(tmp_path) is False


# ── get_head_sha() ────────────────────────────────────────────────────────────


def test_get_head_sha_returns_current_head(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"abc1234567890\n"),
    ):
        sha = svc.get_head_sha(tmp_path)
    assert sha == "abc1234567890"


def test_get_head_sha_strips_whitespace(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"  deadbeef  \n"),
    ):
        sha = svc.get_head_sha(tmp_path)
    assert sha == "deadbeef"


def test_get_head_sha_returns_empty_string_on_git_failure(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=128, stdout=b""),
    ):
        sha = svc.get_head_sha(tmp_path)
    assert sha == ""


# ── get_current_branch() ──────────────────────────────────────────────────────


def test_get_current_branch_returns_branch_name(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"main\n", stderr=b""),
    ):
        branch = svc.get_current_branch(tmp_path)
    assert branch == "main"


def test_get_current_branch_strips_whitespace(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(
            returncode=0, stdout=b"  feature-branch  \n", stderr=b""
        ),
    ):
        branch = svc.get_current_branch(tmp_path)
    assert branch == "feature-branch"


def test_get_current_branch_raises_git_command_error_on_failure(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(
            returncode=128, stdout=b"", stderr=b"fatal: not a git repository"
        ),
    ):
        with pytest.raises(GitCommandError):
            svc.get_current_branch(tmp_path)


def test_get_current_branch_raises_git_timeout_error_on_timeout(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.get_current_branch(tmp_path)


# ── checkout_detached() ───────────────────────────────────────────────────────


def test_checkout_detached_creates_worktree_at_path_with_detach_and_sha(tmp_path):
    svc = GitService(_cfg)
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        svc.checkout_detached(tmp_path, tmp_path / "wt", "deadbeef")

    add_cmd = next(c for c in captured if "worktree" in c and "add" in c)
    assert "--detach" in add_cmd
    assert "deadbeef" in add_cmd
    assert any("prune" in c for c in captured)


def test_checkout_detached_force_removes_existing_path_then_retries(tmp_path):
    svc = GitService(_cfg)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        svc.checkout_detached(tmp_path, worktree, "deadbeef")

    cmds_str = [" ".join(c) for c in captured]
    assert any("worktree remove" in c for c in cmds_str)
    assert any("--detach" in c for c in cmds_str)


def test_checkout_detached_raises_git_command_error_on_add_failure(tmp_path):
    svc = GitService(_cfg)

    def fake_run(cmd, **kwargs):
        if "add" in cmd and "--detach" in cmd:
            return MagicMock(returncode=128, stdout=b"", stderr=b"fatal: bad sha")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(GitCommandError):
            svc.checkout_detached(tmp_path, tmp_path / "wt", "badbad")


def test_checkout_detached_raises_git_timeout_error_on_timeout(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.checkout_detached(tmp_path, tmp_path / "wt", "deadbeef")


# ── fast_forward_branch() ─────────────────────────────────────────────────────


def test_fast_forward_branch_runs_ff_only_merge(tmp_path):
    svc = GitService(_cfg)
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        svc.fast_forward_branch(tmp_path, "main", "pycastle/merge-sandbox")

    assert any(
        "--ff-only" in cmd and "pycastle/merge-sandbox" in cmd for cmd in captured
    )


def test_fast_forward_branch_raises_git_command_error_on_merge_failure(tmp_path):
    svc = GitService(_cfg)

    def fake_run(cmd, **kwargs):
        if "--ff-only" in cmd:
            return MagicMock(
                returncode=1, stdout=b"", stderr=b"Not possible to fast-forward"
            )
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(GitCommandError):
            svc.fast_forward_branch(tmp_path, "main", "pycastle/merge-sandbox")


def test_fast_forward_branch_raises_git_command_error_on_checkout_failure(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(
            returncode=1,
            stdout=b"",
            stderr=b"error: pathspec 'main' did not match any file(s)",
        ),
    ):
        with pytest.raises(GitCommandError):
            svc.fast_forward_branch(tmp_path, "main", "pycastle/merge-sandbox")


def test_fast_forward_branch_raises_git_timeout_error_on_timeout(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.fast_forward_branch(tmp_path, "main", "pycastle/merge-sandbox")


# ── checkout_detached() additional edge cases ─────────────────────────────────


def test_checkout_detached_raises_git_command_error_when_remove_fails(tmp_path):
    svc = GitService(_cfg)
    worktree = tmp_path / "wt"
    worktree.mkdir()

    def fake_run(cmd, **kwargs):
        if "remove" in cmd and "worktree" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"not a worktree")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(GitCommandError):
            svc.checkout_detached(tmp_path, worktree, "deadbeef")


# ── remove_worktree() additional edge cases ───────────────────────────────────


def test_remove_worktree_silent_fallback_when_path_absent(tmp_path):
    svc = GitService(_cfg)
    worktree = tmp_path / "nonexistent-wt"

    with patch("subprocess.run", return_value=MagicMock(returncode=1, stderr=b"error")):
        svc.remove_worktree(tmp_path, worktree)  # must not raise


# ── list_worktrees() additional edge cases ────────────────────────────────────


def test_list_worktrees_skips_non_worktree_lines(tmp_path):
    svc = GitService(_cfg)
    output = b"HEAD abc123\nbranch refs/heads/main\n\n"
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=output)):
        result = svc.list_worktrees(tmp_path)
    assert result == []


# ── create_worktree() additional edge cases ───────────────────────────────────


def test_create_worktree_continues_when_prune_fails(tmp_path):
    svc = GitService(_cfg)
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        if "prune" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"prune failed")
        if "rev-parse" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        svc.create_worktree(tmp_path, tmp_path / "wt", "feature/new")

    assert any("add" in c and "worktree" in c for c in captured)


# ── is_working_tree_clean() additional edge cases ─────────────────────────────


def test_is_working_tree_clean_returns_true_when_status_command_fails(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=128, stdout=b""),
    ):
        assert svc.is_working_tree_clean(tmp_path) is True


# ── pull() ────────────────────────────────────────────────────────────────────


def test_pull_runs_ff_only_flag(tmp_path):
    svc = GitService(_cfg)
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return MagicMock(returncode=0, stdout=b"Already up to date.\n", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        svc.pull(tmp_path)

    assert len(captured) == 1
    assert captured[0] == ["git", "pull", "--ff-only"]


def test_pull_succeeds_on_zero_exit(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(
            returncode=0, stdout=b"Already up to date.\n", stderr=b""
        ),
    ):
        svc.pull(tmp_path)  # must not raise


def test_pull_raises_git_command_error_on_nonzero_exit(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(
            returncode=1, stdout=b"fatal: diverged\n", stderr=b"hint: use rebase"
        ),
    ):
        with pytest.raises(GitCommandError):
            svc.pull(tmp_path)


def test_pull_raises_git_timeout_error_on_timeout(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.pull(tmp_path)
