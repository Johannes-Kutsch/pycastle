import asyncio
import logging
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
    OperatorActionableGitError,
    UnrelatedHistoriesError,
)

_cfg = Config()


def _git_result(
    *, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""
) -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


def _git_failure(
    stderr: bytes, *, returncode: int = 1, stdout: bytes = b""
) -> MagicMock:
    return _git_result(returncode=returncode, stdout=stdout, stderr=stderr)


def _run_pull(svc: GitService, repo_path: Path) -> None:
    svc.pull(repo_path)


def _run_fetch(svc: GitService, repo_path: Path) -> None:
    svc.fetch(repo_path)


def _run_push(svc: GitService, repo_path: Path) -> None:
    asyncio.run(svc.push(repo_path))


_PERMISSION_DENIED_STDERR = b"Permission denied (publickey)."
_NON_FAST_FORWARD_PUSH_STDERR = (
    b"! [rejected] main -> main (fetch first)\nerror: failed to push some refs"
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


def test_git_command_error_str_includes_stderr_and_returncode():
    err = GitCommandError(
        "git pull --ff-only failed",
        128,
        "fatal: Not possible to fast-forward, aborting.",
    )
    s = str(err)
    assert "fatal: Not possible to fast-forward, aborting." in s
    assert "128" in s


def test_git_command_error_str_omits_stderr_section_when_empty():
    err = GitCommandError("git push failed", 1, "")
    assert "stderr:" not in str(err)


def test_git_command_error_str_first_line_is_original_message():
    err = GitCommandError("git pull --ff-only failed", 128, "fatal: error")
    assert str(err).splitlines()[0] == "git pull --ff-only failed"


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


def test_try_merge_raises_unrelated_histories_error_on_unrelated_histories():
    svc = GitService(_cfg)
    merge_result = MagicMock(
        returncode=128,
        stdout=b"",
        stderr=b"fatal: refusing to merge unrelated histories",
    )
    with patch("subprocess.run", return_value=merge_result):
        with pytest.raises(UnrelatedHistoriesError):
            svc.try_merge(Path("/repo"), "origin/main")


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


# ── get_github_remote_repo() ──────────────────────────────────────────────────


def _stub_remote(url: str):
    return patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=url.encode() + b"\n", stderr=b""),
    )


def test_get_github_remote_repo_parses_https_with_dot_git():
    svc = GitService(_cfg)
    with _stub_remote("https://github.com/owner/repo.git"):
        assert svc.get_github_remote_repo() == ("owner", "repo")


def test_get_github_remote_repo_parses_https_without_dot_git():
    svc = GitService(_cfg)
    with _stub_remote("https://github.com/owner/repo"):
        assert svc.get_github_remote_repo() == ("owner", "repo")


def test_get_github_remote_repo_parses_ssh_with_dot_git():
    svc = GitService(_cfg)
    with _stub_remote("git@github.com:owner/repo.git"):
        assert svc.get_github_remote_repo() == ("owner", "repo")


def test_get_github_remote_repo_parses_ssh_without_dot_git():
    svc = GitService(_cfg)
    with _stub_remote("git@github.com:owner/repo"):
        assert svc.get_github_remote_repo() == ("owner", "repo")


def test_get_github_remote_repo_returns_none_for_gitlab():
    svc = GitService(_cfg)
    with _stub_remote("https://gitlab.com/owner/repo.git"):
        assert svc.get_github_remote_repo() is None


def test_get_github_remote_repo_returns_none_for_bitbucket():
    svc = GitService(_cfg)
    with _stub_remote("git@bitbucket.org:owner/repo.git"):
        assert svc.get_github_remote_repo() is None


def test_get_github_remote_repo_returns_none_when_remote_missing():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        return_value=MagicMock(
            returncode=2, stdout=b"", stderr=b"error: No such remote 'origin'"
        ),
    ):
        assert svc.get_github_remote_repo() is None


def test_get_github_remote_repo_returns_none_on_timeout():
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        assert svc.get_github_remote_repo() is None


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


def test_create_worktree_succeeds_when_orphan_dir_exists_at_path(tmp_path):
    svc = GitService(_cfg)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "leftover.txt").write_text("from a prior crashed run")
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        if "remove" in cmd and "worktree" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"not a working tree")
        if "rev-parse" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        svc.create_worktree(tmp_path, worktree, "feature/new")

    assert not worktree.exists()
    assert any("add" in c and "worktree" in c for c in captured)


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


def test_checkout_detached_succeeds_when_orphan_dir_exists_at_path(tmp_path):
    svc = GitService(_cfg)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "leftover.txt").write_text("from a prior crashed run")
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        if "remove" in cmd and "worktree" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"not a working tree")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        svc.checkout_detached(tmp_path, worktree, "deadbeef")

    assert not worktree.exists()
    assert any("--detach" in c for c in captured)


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


def test_pull_raises_operator_actionable_error_after_exhausting_retries(tmp_path):
    svc = GitService(_cfg)
    with (
        patch(
            "subprocess.run",
            return_value=MagicMock(
                returncode=1, stdout=b"fatal: diverged\n", stderr=b"hint: use rebase"
            ),
        ),
        patch("time.sleep"),
    ):
        with pytest.raises(OperatorActionableGitError) as exc_info:
            svc.pull(tmp_path)
    assert exc_info.value.op == "pull"
    assert exc_info.value.attempt_count == 4


def test_pull_raises_git_timeout_error_on_timeout(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.pull(tmp_path)


# ── commit() ──────────────────────────────────────────────────────────────────


def test_commit_runs_add_then_commit_with_message(tmp_path):
    svc = GitService(_cfg)
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        if "diff" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        result = svc.commit(tmp_path / "wt", tmp_path, "RALPH: Implement - foo")

    assert result is True
    assert captured[0] == ["git", "-C", str(tmp_path / "wt"), "add", "-A"]
    assert captured[-1] == [
        "git",
        "-C",
        str(tmp_path / "wt"),
        "commit",
        "-m",
        "RALPH: Implement - foo",
    ]


def test_commit_returns_false_and_skips_commit_when_nothing_staged(tmp_path):
    svc = GitService(_cfg)
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        result = svc.commit(tmp_path / "wt", tmp_path, "RALPH: Implement - foo")

    assert result is False
    assert all("commit" not in cmd for cmd in captured)


def test_commit_raises_git_command_error_on_add_failure(tmp_path):
    svc = GitService(_cfg)
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        if "add" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"add error")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(GitCommandError):
            svc.commit(tmp_path / "wt", tmp_path, "msg")

    assert all("commit" not in cmd for cmd in captured)


def test_commit_raises_git_command_error_on_commit_failure(tmp_path):
    svc = GitService(_cfg)

    def fake_run(cmd, **kwargs):
        if "commit" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"commit failed")
        if "diff" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(GitCommandError):
            svc.commit(tmp_path / "wt", tmp_path, "msg")


def test_commit_raises_git_timeout_error_on_timeout(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.commit(tmp_path / "wt", tmp_path, "msg")


# ── push() ────────────────────────────────────────────────────────────────────


def test_push_runs_git_push(tmp_path):
    svc = GitService(_cfg)
    captured: list[tuple[list[str], object]] = []

    def fake_run(cmd, **kwargs):
        captured.append((list(cmd), kwargs.get("cwd")))
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        asyncio.run(svc.push(tmp_path))

    assert len(captured) == 1
    cmd, cwd = captured[0]
    assert cmd == ["git", "push"]
    assert cwd == tmp_path


def test_push_succeeds_on_zero_exit(tmp_path):
    svc = GitService(_cfg)
    with patch("subprocess.run", return_value=_git_result()):
        asyncio.run(svc.push(tmp_path))  # must not raise


def test_push_raises_operator_actionable_error_after_exhausting_retries(tmp_path):
    svc = GitService(_cfg)
    with (
        patch(
            "subprocess.run",
            return_value=_git_failure(b"network error"),
        ),
        patch("time.sleep"),
    ):
        with pytest.raises(OperatorActionableGitError) as exc_info:
            asyncio.run(svc.push(tmp_path))
    assert exc_info.value.op == "push"
    assert exc_info.value.attempt_count == 4


def test_push_raises_git_timeout_error_on_timeout(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            asyncio.run(svc.push(tmp_path))


# ── retry behaviour (pull / push / fetch) ─────────────────────────────────────


def test_pull_retries_on_transient_failure_and_succeeds(tmp_path):
    svc = GitService(_cfg)
    responses = iter(
        [
            _git_failure(b"RPC failed; connection reset"),
            _git_failure(b"Another git process seems to be running"),
            _git_result(stdout=b"Already up to date.\n"),
        ]
    )

    with (
        patch("subprocess.run", side_effect=lambda *a, **kw: next(responses)),
        patch("time.sleep") as mock_sleep,
    ):
        svc.pull(tmp_path)  # must not raise

    assert mock_sleep.call_count == 2
    assert mock_sleep.call_args_list[0][0][0] == 10
    assert mock_sleep.call_args_list[1][0][0] == 60


def test_pull_raises_git_command_error_immediately_on_divergence(tmp_path):
    svc = GitService(_cfg)
    attempts = 0

    def fake_run(*a, **kw):
        nonlocal attempts
        attempts += 1
        return _git_failure(b"fatal: Not possible to fast-forward, aborting.")

    with (
        patch("subprocess.run", side_effect=fake_run),
        patch("time.sleep") as mock_sleep,
    ):
        with pytest.raises(GitCommandError):
            svc.pull(tmp_path)

    assert attempts == 1
    mock_sleep.assert_not_called()


@pytest.mark.parametrize(
    ("run_remote_op", "op", "returncode"),
    [
        pytest.param(_run_pull, "pull", 1, id="pull"),
        pytest.param(_run_fetch, "fetch", 1, id="fetch"),
        pytest.param(_run_push, "push", 128, id="push"),
    ],
)
def test_remote_op_raises_operator_actionable_error_immediately_for_stable_misconfig(
    tmp_path, run_remote_op, op, returncode
):
    svc = GitService(_cfg)
    attempts = 0
    stderr = b"remote: Repository not found."

    def fake_run(*a, **kw):
        nonlocal attempts
        attempts += 1
        return _git_failure(stderr, returncode=returncode)

    with (
        patch("subprocess.run", side_effect=fake_run),
        patch("time.sleep") as mock_sleep,
    ):
        with pytest.raises(OperatorActionableGitError) as exc_info:
            run_remote_op(svc, tmp_path)

    assert attempts == 1
    assert exc_info.value.op == op
    assert exc_info.value.attempt_count == 1
    assert exc_info.value.stderr == stderr.decode()
    mock_sleep.assert_not_called()


def test_pull_final_exception_carries_last_attempt_stderr(tmp_path):
    svc = GitService(_cfg)
    responses = iter(
        [
            _git_failure(b"transient error attempt 1"),
            _git_failure(b"transient error attempt 2"),
            _git_failure(b"transient error attempt 3"),
            _git_failure(b"transient error attempt 4"),
        ]
    )

    with (
        patch("subprocess.run", side_effect=lambda *a, **kw: next(responses)),
        patch("time.sleep"),
    ):
        with pytest.raises(OperatorActionableGitError) as exc_info:
            svc.pull(tmp_path)

    assert exc_info.value.stderr == "transient error attempt 4"


def test_pull_successful_retry_emits_warning(tmp_path, caplog):
    svc = GitService(_cfg)
    responses = iter(
        [
            _git_failure(b"transient network error"),
            _git_result(stdout=b"Already up to date.\n"),
        ]
    )

    with (
        patch("subprocess.run", side_effect=lambda *a, **kw: next(responses)),
        patch("time.sleep"),
        caplog.at_level(logging.WARNING, logger="pycastle.services.git_service"),
    ):
        svc.pull(tmp_path)

    assert any(
        "pull" in record.message and "2" in record.message for record in caplog.records
    )


def test_pull_timeout_error_is_not_retried(tmp_path):
    svc = GitService(_cfg)
    call_count = 0

    def fake_run(*a, **kw):
        nonlocal call_count
        call_count += 1
        raise subprocess.TimeoutExpired(cmd="git", timeout=30)

    with (
        patch("subprocess.run", side_effect=fake_run),
        patch("time.sleep") as mock_sleep,
    ):
        with pytest.raises(GitTimeoutError):
            svc.pull(tmp_path)

    assert call_count == 1
    mock_sleep.assert_not_called()


def test_pull_not_found_error_is_not_retried(tmp_path):
    svc = GitService(_cfg)
    call_count = 0

    def fake_run(*a, **kw):
        nonlocal call_count
        call_count += 1
        raise FileNotFoundError

    with (
        patch("subprocess.run", side_effect=fake_run),
        patch("time.sleep") as mock_sleep,
    ):
        with pytest.raises(GitNotFoundError):
            svc.pull(tmp_path)

    assert call_count == 1
    mock_sleep.assert_not_called()


def test_push_retries_on_transient_failure(tmp_path):
    svc = GitService(_cfg)
    responses = iter(
        [
            _git_failure(b"error: failed to push some refs"),
            _git_result(),
        ]
    )

    with (
        patch("subprocess.run", side_effect=lambda *a, **kw: next(responses)),
        patch("time.sleep") as mock_sleep,
    ):
        asyncio.run(svc.push(tmp_path))  # must not raise

    assert mock_sleep.call_count == 1


def test_push_successful_retry_emits_warning(tmp_path, caplog):
    svc = GitService(_cfg)
    responses = iter(
        [
            _git_failure(b"transient network error"),
            _git_result(),
        ]
    )

    with (
        patch("subprocess.run", side_effect=lambda *a, **kw: next(responses)),
        patch("time.sleep"),
        caplog.at_level(logging.WARNING, logger="pycastle.services.git_service"),
    ):
        asyncio.run(svc.push(tmp_path))

    assert any(
        "push" in record.message and "2" in record.message for record in caplog.records
    )


def test_push_raises_after_four_non_fast_forward_rejections(tmp_path):
    """All 4 push attempts rejected → raises GitCommandError with final stderr."""
    svc = GitService(_cfg)
    push_count = 0

    def fake_run(cmd, **kwargs):
        nonlocal push_count
        if cmd == ["git", "push"]:
            push_count += 1
            return _git_failure(_NON_FAST_FORWARD_PUSH_STDERR)
        return _git_result()

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(GitCommandError) as exc_info:
            asyncio.run(svc.push(tmp_path))

    assert push_count == 4
    assert "[rejected]" in exc_info.value.stderr


# ── push() — non-fast-forward recovery ───────────────────────────────────────


def test_push_pulls_with_merge_fallback_on_rejection_then_succeeds(tmp_path):
    """Push rejected non-fast-forward → pull_with_merge_fallback → retry push succeeds."""
    svc = GitService(_cfg)
    captured: list[list[str]] = []
    nff_stderr = (
        b" ! [rejected]        main -> main (fetch first)\n"
        b"error: failed to push some refs to 'github.com:owner/repo.git'"
    )

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        push_count = sum(1 for c in captured if c == ["git", "push"])
        if cmd == ["git", "push"] and push_count == 1:
            return _git_failure(nff_stderr)
        return _git_result()

    with patch("subprocess.run", side_effect=fake_run):
        asyncio.run(svc.push(tmp_path))  # must not raise

    push_indices = [i for i, c in enumerate(captured) if c == ["git", "push"]]
    assert len(push_indices) == 2, f"Expected 2 push calls, got: {captured}"
    between = captured[push_indices[0] + 1 : push_indices[1]]
    assert any(c == ["git", "pull", "--ff-only"] for c in between), (
        f"Expected pull --ff-only between pushes, got: {between}"
    )
    assert not any("rebase" in c for c in between), (
        f"Expected no rebase between pushes, got: {between}"
    )


def test_push_raises_when_pull_with_merge_fallback_fails_on_conflict(tmp_path):
    """If pull_with_merge_fallback raises conflict after push rejection, push raises."""
    svc = GitService(_cfg)

    def fake_run(cmd, **kwargs):
        if cmd == ["git", "push"]:
            return _git_failure(_NON_FAST_FORWARD_PUSH_STDERR)
        if cmd == ["git", "pull", "--ff-only"]:
            return _git_failure(b"fatal: Not possible to fast-forward, aborting.")
        if cmd[:2] == ["git", "merge"]:
            return _git_failure(b"CONFLICT (content): conflict in file.py")
        return _git_result()

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(GitCommandError):
            asyncio.run(svc.push(tmp_path))


def test_push_does_not_fetch_rebase_on_transient_failure(tmp_path):
    """Transient network failures still use sleep+retry without fetch+rebase."""
    svc = GitService(_cfg)
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        if (
            cmd == ["git", "push"]
            and sum(1 for c in captured if c == ["git", "push"]) == 1
        ):
            return MagicMock(
                returncode=1, stdout=b"", stderr=b"RPC failed; connection reset"
            )
        return _git_result()

    with (
        patch("subprocess.run", side_effect=fake_run),
        patch("time.sleep") as mock_sleep,
    ):
        asyncio.run(svc.push(tmp_path))  # must not raise

    assert mock_sleep.call_count == 1
    assert not any("fetch" in c for c in captured)
    assert not any("rebase" in c for c in captured)


def test_push_uses_pull_with_merge_fallback_on_nff_rejection(tmp_path):
    """On NFF push rejection, push calls pull_with_merge_fallback instead of git rebase."""
    svc = GitService(_cfg)
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        push_count = sum(1 for c in captured if c == ["git", "push"])
        if cmd == ["git", "push"] and push_count == 1:
            return _git_failure(_NON_FAST_FORWARD_PUSH_STDERR)
        return _git_result()

    with patch("subprocess.run", side_effect=fake_run):
        asyncio.run(svc.push(tmp_path))  # must not raise

    assert not any("rebase" in c for c in captured), "git rebase must not be called"
    assert any(c == ["git", "pull", "--ff-only"] for c in captured), (
        "git pull --ff-only must be called"
    )


def test_push_retries_non_fast_forward_recovery_without_sleep(tmp_path):
    svc = GitService(_cfg)
    push_attempt = 0

    def fake_run(cmd, **kwargs):
        nonlocal push_attempt
        if cmd == ["git", "push"]:
            push_attempt += 1
            if push_attempt == 1:
                return _git_failure(_NON_FAST_FORWARD_PUSH_STDERR)
            return _git_result()
        return _git_result(stdout=b"Already up to date.\n")

    with (
        patch("subprocess.run", side_effect=fake_run),
        patch("time.sleep") as mock_sleep,
    ):
        asyncio.run(svc.push(tmp_path))

    assert push_attempt == 2
    mock_sleep.assert_not_called()


def test_push_calls_async_resolver_on_textual_conflict_and_retries(tmp_path):
    """When pull_with_merge_fallback raises a textual conflict, the async resolver is called and push retries."""
    svc = GitService(_cfg)
    resolver_called = False
    push_attempt = 0

    async def resolver() -> None:
        nonlocal resolver_called
        resolver_called = True

    def fake_run(cmd, **kwargs):
        nonlocal push_attempt
        if cmd == ["git", "push"]:
            push_attempt += 1
            if push_attempt == 1:
                return _git_failure(_NON_FAST_FORWARD_PUSH_STDERR)
            return _git_result()
        if cmd == ["git", "pull", "--ff-only"]:
            return _git_failure(b"fatal: Not possible to fast-forward, aborting.")
        if cmd == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return _git_result(stdout=b"main\n")
        if len(cmd) >= 4 and cmd[:3] == ["git", "merge", "--no-edit"]:
            return _git_failure(b"CONFLICT (content): Merge conflict in file.py")
        return _git_result()

    with patch("subprocess.run", side_effect=fake_run):
        asyncio.run(svc.push(tmp_path, resolver=resolver))

    assert resolver_called, "async resolver must be called on textual conflict"
    assert push_attempt == 2, f"push must be retried after resolver, got {push_attempt}"


# ── push() — integration with real git repos ─────────────────────────────────


def _clone_with_user(src: Path, dest: Path) -> None:
    subprocess.run(
        ["git", "clone", str(src), str(dest)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=dest,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=dest,
        check=True,
        capture_output=True,
    )


def _make_push_scenario(tmp_path: Path) -> tuple[Path, Path, str]:
    """Return (local, bare_remote, merge_sha): local has an unpushed merge commit; bare_remote has diverged."""
    seed = tmp_path / "seed"
    _init_repo(seed)
    bare = tmp_path / "bare.git"
    subprocess.run(
        ["git", "clone", "--bare", str(seed), str(bare)],
        check=True,
        capture_output=True,
    )

    local = tmp_path / "local"
    _clone_with_user(bare, local)

    # local: add a feature branch and merge it (simulating pycastle's merge commit)
    subprocess.run(
        ["git", "checkout", "-b", "feature"], cwd=local, check=True, capture_output=True
    )
    (local / "feature.txt").write_text("feature\n")
    subprocess.run(["git", "add", "."], cwd=local, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "feature commit"],
        cwd=local,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "main"], cwd=local, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "merge", "--no-ff", "feature", "-m", "Merge feature"],
        cwd=local,
        check=True,
        capture_output=True,
    )
    merge_sha = (
        subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=local, check=True, capture_output=True
        )
        .stdout.strip()
        .decode()
    )

    # a second clone pushes a non-conflicting commit to bare → local is now behind
    second = tmp_path / "second"
    _clone_with_user(bare, second)
    (second / "other.txt").write_text("other change\n")
    subprocess.run(["git", "add", "."], cwd=second, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "remote commit"],
        cwd=second,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "push"], cwd=second, check=True, capture_output=True)

    return local, bare, merge_sha


def test_push_preserves_merge_commits_after_nff_rejection(tmp_path):
    """Local merge commits must remain in history after push recovers from NFF rejection."""
    local, remote, merge_sha = _make_push_scenario(tmp_path)
    svc = GitService(_cfg)

    asyncio.run(svc.push(local))  # must not raise

    # verify the merge commit is present in local main history
    log = subprocess.run(
        ["git", "log", "--format=%H", "main"],
        cwd=local,
        check=True,
        capture_output=True,
    ).stdout.decode()
    assert merge_sha in log, "merge commit must remain in history after push"

    # verify local main is now present in remote
    remote_log = subprocess.run(
        ["git", "log", "--format=%H", "main"],
        cwd=remote,
        check=True,
        capture_output=True,
    ).stdout.decode()
    assert merge_sha in remote_log, "merge commit must reach remote after push"


# ── fetch() ───────────────────────────────────────────────────────────────────


def test_fetch_succeeds_on_zero_exit(tmp_path):
    svc = GitService(_cfg)
    with patch("subprocess.run", return_value=_git_result()):
        svc.fetch(tmp_path)  # must not raise


def test_fetch_raises_operator_actionable_error_after_exhausting_retries(tmp_path):
    svc = GitService(_cfg)
    with (
        patch(
            "subprocess.run",
            return_value=_git_failure(b"network error"),
        ),
        patch("time.sleep"),
    ):
        with pytest.raises(OperatorActionableGitError) as exc_info:
            svc.fetch(tmp_path)
    assert exc_info.value.op == "fetch"
    assert exc_info.value.attempt_count == 4


def test_fetch_final_exception_carries_last_attempt_stderr(tmp_path):
    svc = GitService(_cfg)
    responses = iter(
        [
            _git_failure(b"transient error attempt 1"),
            _git_failure(b"transient error attempt 2"),
            _git_failure(b"transient error attempt 3"),
            _git_failure(b"transient error attempt 4"),
        ]
    )

    with (
        patch("subprocess.run", side_effect=lambda *a, **kw: next(responses)),
        patch("time.sleep"),
    ):
        with pytest.raises(OperatorActionableGitError) as exc_info:
            svc.fetch(tmp_path)

    assert exc_info.value.stderr == "transient error attempt 4"


def test_fetch_retries_on_transient_failure(tmp_path):
    svc = GitService(_cfg)
    responses = iter(
        [
            _git_failure(b"error: RPC failed; curl 56"),
            _git_result(),
        ]
    )

    with (
        patch("subprocess.run", side_effect=lambda *a, **kw: next(responses)),
        patch("time.sleep") as mock_sleep,
    ):
        svc.fetch(tmp_path)  # must not raise

    assert mock_sleep.call_count == 1


def test_fetch_successful_retry_emits_warning(tmp_path, caplog):
    svc = GitService(_cfg)
    responses = iter(
        [
            _git_failure(b"transient network error"),
            _git_result(),
        ]
    )

    with (
        patch("subprocess.run", side_effect=lambda *a, **kw: next(responses)),
        patch("time.sleep"),
        caplog.at_level(logging.WARNING, logger="pycastle.services.git_service"),
    ):
        svc.fetch(tmp_path)

    assert any(
        "fetch" in record.message and "2" in record.message for record in caplog.records
    )


def test_fetch_retries_on_auth_failure_and_raises_operator_actionable_on_exhaustion(
    tmp_path,
):
    svc = GitService(_cfg)
    attempts = 0

    def fake_run(*a, **kw):
        nonlocal attempts
        attempts += 1
        return _git_failure(b"remote: Authentication failed", returncode=128)

    with (
        patch("subprocess.run", side_effect=fake_run),
        patch("time.sleep") as mock_sleep,
    ):
        with pytest.raises(OperatorActionableGitError) as exc_info:
            svc.fetch(tmp_path)

    assert attempts == 4
    assert mock_sleep.call_count == 3
    assert exc_info.value.op == "fetch"
    assert exc_info.value.attempt_count == 4


def test_fetch_retries_permission_denied_and_succeeds_on_second_attempt(tmp_path):
    svc = GitService(_cfg)
    responses = iter(
        [
            _git_failure(_PERMISSION_DENIED_STDERR),
            _git_result(),
        ]
    )

    with (
        patch("subprocess.run", side_effect=lambda *a, **kw: next(responses)),
        patch("time.sleep") as mock_sleep,
    ):
        svc.fetch(tmp_path)  # must not raise

    mock_sleep.assert_called_once()


def test_fetch_raises_git_command_error_immediately_for_divergence(tmp_path):
    svc = GitService(_cfg)
    attempts = 0
    stderr = b"fatal: refusing to merge unrelated histories"

    def fake_run(*a, **kw):
        nonlocal attempts
        attempts += 1
        return _git_failure(stderr)

    with (
        patch("subprocess.run", side_effect=fake_run),
        patch("time.sleep") as mock_sleep,
    ):
        with pytest.raises(GitCommandError) as exc_info:
            svc.fetch(tmp_path)

    assert attempts == 1
    assert exc_info.value.stderr == stderr.decode()
    mock_sleep.assert_not_called()


def test_fetch_raises_git_timeout_error_on_timeout(tmp_path):
    svc = GitService(_cfg)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with pytest.raises(GitTimeoutError):
            svc.fetch(tmp_path)


# ── pull_with_merge_fallback() ────────────────────────────────────────────────


def _init_repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """Create a remote repo and a clone (local) with one shared initial commit."""
    remote = tmp_path / "remote"
    _init_repo(remote)

    local = tmp_path / "local"
    subprocess.run(
        ["git", "clone", str(remote), str(local)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=local,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=local,
        check=True,
        capture_output=True,
    )
    return local, remote


def test_pull_with_merge_fallback_produces_merge_commit_on_non_conflicting_divergence(
    tmp_path,
):
    local, remote = _init_repo_with_remote(tmp_path)

    # remote gets a new commit (someone pushed)
    (remote / "remote.txt").write_text("remote change\n")
    subprocess.run(["git", "add", "."], cwd=remote, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "remote commit"],
        cwd=remote,
        check=True,
        capture_output=True,
    )

    # local has an unpushed commit (diverged)
    (local / "local.txt").write_text("local change\n")
    subprocess.run(["git", "add", "."], cwd=local, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "local commit"],
        cwd=local,
        check=True,
        capture_output=True,
    )

    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=local, check=True, capture_output=True
    ).stdout.strip()

    svc = GitService(_cfg)
    svc.pull_with_merge_fallback(local)  # must not raise

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=local, check=True, capture_output=True
    ).stdout.strip()

    assert head_after != head_before

    # HEAD must be a merge commit (two parents)
    parents = (
        subprocess.run(
            ["git", "log", "--pretty=%P", "-1"],
            cwd=local,
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
        .split()
    )
    assert len(parents) == 2


def test_pull_with_merge_fallback_fast_forwards_without_merge_commit(tmp_path):
    local, remote = _init_repo_with_remote(tmp_path)

    # remote gets a new commit; local has no diverging commits
    (remote / "remote.txt").write_text("remote change\n")
    subprocess.run(["git", "add", "."], cwd=remote, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "remote commit"],
        cwd=remote,
        check=True,
        capture_output=True,
    )

    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=local, check=True, capture_output=True
    ).stdout.strip()

    svc = GitService(_cfg)
    svc.pull_with_merge_fallback(local)  # must not raise

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=local, check=True, capture_output=True
    ).stdout.strip()

    assert head_after != head_before

    # HEAD must be a plain commit (one parent), not a merge commit
    parents = (
        subprocess.run(
            ["git", "log", "--pretty=%P", "-1"],
            cwd=local,
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
        .split()
    )
    assert len(parents) == 1


def test_pull_with_merge_fallback_raises_git_command_error_on_conflict(tmp_path):
    local, remote = _init_repo_with_remote(tmp_path)

    # remote edits base.txt
    (remote / "base.txt").write_text("remote change\n")
    subprocess.run(["git", "add", "."], cwd=remote, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "remote edit"],
        cwd=remote,
        check=True,
        capture_output=True,
    )

    # local edits the same file with conflicting content
    (local / "base.txt").write_text("local change\n")
    subprocess.run(["git", "add", "."], cwd=local, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "local edit"],
        cwd=local,
        check=True,
        capture_output=True,
    )

    svc = GitService(_cfg)
    with pytest.raises(GitCommandError):
        svc.pull_with_merge_fallback(local)

    # working tree must be clean (no merge state, no conflict markers)
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=local, check=True, capture_output=True
    ).stdout.strip()
    assert status == b""


# ── OperatorActionableGitError / retry-then-escalate ─────────────────────────


def test_pull_retries_permission_denied_and_succeeds_on_second_attempt(tmp_path):
    svc = GitService(_cfg)
    responses = iter(
        [
            _git_failure(_PERMISSION_DENIED_STDERR),
            _git_result(stdout=b"Already up to date.\n"),
        ]
    )

    with (
        patch("subprocess.run", side_effect=lambda *a, **kw: next(responses)),
        patch("time.sleep") as mock_sleep,
    ):
        svc.pull(tmp_path)  # must not raise

    mock_sleep.assert_called_once()


def test_pull_raises_operator_actionable_error_when_all_four_attempts_permission_denied(
    tmp_path,
):
    svc = GitService(_cfg)
    attempts = 0

    def fake_run(*a, **kw):
        nonlocal attempts
        attempts += 1
        return _git_failure(_PERMISSION_DENIED_STDERR)

    with (
        patch("subprocess.run", side_effect=fake_run),
        patch("time.sleep"),
    ):
        with pytest.raises(OperatorActionableGitError) as exc_info:
            svc.pull(tmp_path)

    assert attempts == 4
    assert exc_info.value.op == "pull"
    assert exc_info.value.attempt_count == 4
    assert "Permission denied" in exc_info.value.stderr


def test_push_raises_operator_actionable_error_after_four_permission_denied_attempts(
    tmp_path,
):
    svc = GitService(_cfg)
    attempts = 0

    def fake_run(*a, **kw):
        nonlocal attempts
        attempts += 1
        return _git_failure(_PERMISSION_DENIED_STDERR, returncode=128)

    with (
        patch("subprocess.run", side_effect=fake_run),
        patch("time.sleep"),
    ):
        with pytest.raises(OperatorActionableGitError) as exc_info:
            asyncio.run(svc.push(tmp_path))

    assert attempts == 4
    assert exc_info.value.op == "push"
    assert exc_info.value.attempt_count == 4


def test_push_non_nff_divergence_raises_git_command_error(tmp_path):
    svc = GitService(_cfg)
    attempts = 0
    stderr = b"fatal: refusing to merge unrelated histories"

    def fake_run(*a, **kw):
        nonlocal attempts
        attempts += 1
        return _git_failure(stderr)

    with (
        patch("subprocess.run", side_effect=fake_run),
        patch("time.sleep") as mock_sleep,
    ):
        with pytest.raises(GitCommandError) as exc_info:
            asyncio.run(svc.push(tmp_path))

    assert attempts == 1
    assert exc_info.value.stderr == stderr.decode()
    mock_sleep.assert_not_called()
    assert not isinstance(exc_info.value, OperatorActionableGitError)


def test_operator_actionable_git_error_is_not_subclass_of_git_command_error():
    assert not issubclass(OperatorActionableGitError, GitCommandError)
    assert issubclass(OperatorActionableGitError, GitServiceError)


def test_operator_actionable_git_error_carries_stderr_op_and_attempt_count():
    err = OperatorActionableGitError(
        "git pull failed", stderr="Permission denied", op="pull", attempt_count=4
    )
    assert err.stderr == "Permission denied"
    assert err.op == "pull"
    assert err.attempt_count == 4


def test_pull_with_merge_fallback_retries_transient_blip_on_inner_pull(tmp_path):
    """Inner pull --ff-only retries a transient blip: blip on attempt 1, success on attempt 2."""
    svc = GitService(_cfg)
    responses = iter(
        [
            _git_failure(b"RPC failed; connection reset"),
            _git_result(stdout=b"Already up to date.\n"),
        ]
    )

    with (
        patch("subprocess.run", side_effect=lambda *a, **kw: next(responses)),
        patch("time.sleep") as mock_sleep,
    ):
        svc.pull_with_merge_fallback(tmp_path)  # must not raise

    mock_sleep.assert_called_once()
