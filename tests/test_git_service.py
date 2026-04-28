import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pycastle.git_service import (
    GitCommandError,
    GitNotFoundError,
    GitService,
    GitServiceError,
    GitTimeoutError,
)


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


# ── _run() wrapper ─────────────────────────────────────────────────────────────


def test_run_raises_git_timeout_error_on_subprocess_timeout():
    svc = GitService()
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc._run(["git", "status"])


def test_run_raises_git_not_found_error_when_git_missing():
    svc = GitService()
    with patch("subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(GitNotFoundError):
            svc._run(["git", "status"])


def test_run_returns_completed_process_on_success():
    svc = GitService()
    mock_result = MagicMock(returncode=0, stdout=b"ok\n", stderr=b"")
    with patch("subprocess.run", return_value=mock_result):
        result = svc._run(["git", "status"], capture_output=True)
    assert result.returncode == 0


def test_run_applies_default_timeout():
    svc = GitService(timeout=42)
    mock_result = MagicMock(returncode=0, stdout=b"", stderr=b"")
    with patch("subprocess.run", return_value=mock_result) as m:
        svc._run(["git", "status"])
    assert m.call_args.kwargs.get("timeout") == 42


# ── get_user_name() ────────────────────────────────────────────────────────────


def test_get_user_name_returns_name():
    svc = GitService()
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"Alice\n", stderr=b""),
    ):
        assert svc.get_user_name() == "Alice"


def test_get_user_name_raises_git_command_error_on_failure():
    svc = GitService()
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=1, stdout=b"", stderr=b"error"),
    ):
        with pytest.raises(GitCommandError):
            svc.get_user_name()


def test_get_user_name_raises_git_timeout_error_on_timeout():
    svc = GitService()
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.get_user_name()


def test_get_user_name_strips_trailing_newline():
    svc = GitService()
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"  Bob  \n", stderr=b""),
    ):
        assert svc.get_user_name() == "Bob"


# ── get_user_email() ───────────────────────────────────────────────────────────


def test_get_user_email_returns_email():
    svc = GitService()
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"alice@example.com\n", stderr=b""),
    ):
        assert svc.get_user_email() == "alice@example.com"


def test_get_user_email_raises_git_command_error_on_failure():
    svc = GitService()
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=1, stdout=b"", stderr=b"error"),
    ):
        with pytest.raises(GitCommandError):
            svc.get_user_email()


def test_get_user_email_raises_git_timeout_error_on_timeout():
    svc = GitService()
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.get_user_email()


# ── is_ancestor() ──────────────────────────────────────────────────────────────


def test_is_ancestor_returns_true_when_ancestor():
    svc = GitService()
    with patch("subprocess.run", return_value=MagicMock(returncode=0)):
        assert svc.is_ancestor("main", Path("/tmp/repo")) is True


def test_is_ancestor_returns_false_when_not_ancestor():
    svc = GitService()
    with patch("subprocess.run", return_value=MagicMock(returncode=1)):
        assert svc.is_ancestor("feature/x", Path("/tmp/repo")) is False


def test_is_ancestor_raises_git_timeout_error_on_timeout():
    svc = GitService()
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.is_ancestor("main", Path("/tmp/repo"))


# ── verify_ref_exists() ────────────────────────────────────────────────────────


def test_verify_ref_exists_returns_true_when_ref_present():
    svc = GitService()
    with patch("subprocess.run", return_value=MagicMock(returncode=0)):
        assert svc.verify_ref_exists("main", Path("/tmp/repo")) is True


def test_verify_ref_exists_returns_false_when_ref_absent():
    svc = GitService()
    with patch("subprocess.run", return_value=MagicMock(returncode=1)):
        assert svc.verify_ref_exists("nonexistent", Path("/tmp/repo")) is False


def test_verify_ref_exists_raises_git_timeout_error_on_timeout():
    svc = GitService()
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.verify_ref_exists("main", Path("/tmp/repo"))


# ── delete_branch() ───────────────────────────────────────────────────────────


def test_delete_branch_succeeds_silently():
    svc = GitService()
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr=b"")):
        svc.delete_branch("feature/old", Path("/tmp/repo"))  # must not raise


def test_delete_branch_raises_git_command_error_on_failure():
    svc = GitService()
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=1, stderr=b"error: branch not found"),
    ):
        with pytest.raises(GitCommandError):
            svc.delete_branch("nonexistent", Path("/tmp/repo"))


def test_delete_branch_raises_git_timeout_error_on_timeout():
    svc = GitService()
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
    svc = GitService()
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
    svc = GitService()
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b""),
    ):
        assert svc.list_worktrees(Path("/tmp/repo")) == []


def test_list_worktrees_raises_git_command_error_on_failure():
    svc = GitService()
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=1, stdout=b"", stderr=b"error"),
    ):
        with pytest.raises(GitCommandError):
            svc.list_worktrees(Path("/tmp/repo"))


def test_list_worktrees_raises_git_timeout_error_on_timeout():
    svc = GitService()
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.list_worktrees(Path("/tmp/repo"))


# ── get_remote_url() ──────────────────────────────────────────────────────────


def test_get_remote_url_returns_url():
    svc = GitService()
    with patch(
        "subprocess.run",
        return_value=MagicMock(
            returncode=0,
            stdout=b"https://github.com/owner/repo.git\n",
        ),
    ):
        assert svc.get_remote_url() == "https://github.com/owner/repo.git"


def test_get_remote_url_raises_git_command_error_on_failure():
    svc = GitService()
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=2, stdout=b"", stderr=b"no such remote"),
    ):
        with pytest.raises(GitCommandError):
            svc.get_remote_url()


def test_get_remote_url_raises_git_timeout_error_on_timeout():
    svc = GitService()
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.get_remote_url()


def test_get_remote_url_uses_custom_remote():
    svc = GitService()
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
    svc = GitService()
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
    svc = GitService()
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
    svc = GitService()
    with patch("subprocess.run", side_effect=_make_fake_run(add_rc=1)):
        with pytest.raises(GitCommandError):
            svc.create_worktree(tmp_path, tmp_path / "wt", "feature/conflict")


def test_create_worktree_raises_git_timeout_error_on_timeout(tmp_path):
    svc = GitService()
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.create_worktree(tmp_path, tmp_path / "wt", "feature/new")


def test_create_worktree_continues_after_silent_remove_failure(tmp_path):
    svc = GitService()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        if "remove" in cmd and "worktree" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"not a worktree")
        if "rev-parse" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        svc.create_worktree(tmp_path, worktree, "feature/new")

    cmds_str = [" ".join(c) for c in captured]
    assert any("worktree add" in c for c in cmds_str)


def test_create_worktree_removes_existing_dir_before_add(tmp_path):
    svc = GitService()
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


# ── remove_worktree() ─────────────────────────────────────────────────────────


def test_remove_worktree_calls_git_worktree_remove(tmp_path):
    svc = GitService()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr=b"")) as m:
        svc.remove_worktree(tmp_path, worktree)
    cmd = m.call_args[0][0]
    assert "worktree" in cmd and "remove" in cmd


def test_remove_worktree_falls_back_to_rmtree_when_git_fails(tmp_path):
    svc = GitService()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "file.txt").write_text("content")

    with patch("subprocess.run", return_value=MagicMock(returncode=1, stderr=b"error")):
        svc.remove_worktree(tmp_path, worktree)

    assert not worktree.exists()


def test_remove_worktree_raises_git_timeout_error_on_timeout(tmp_path):
    svc = GitService()
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.remove_worktree(tmp_path, tmp_path / "wt")
