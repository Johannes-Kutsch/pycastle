import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from pycastle.errors import WorktreeTimeoutError
from pycastle.git_service import GitService, GitTimeoutError
from pycastle.worktree import (
    create_worktree,
    patch_gitdir_for_container,
    remove_worktree,
)


# ── Cycle 23-1: timeout constants ────────────────────────────────────────────


def test_worktree_timeout_constant_exists():
    from pycastle.defaults.config import WORKTREE_TIMEOUT

    assert WORKTREE_TIMEOUT == 30


def test_idle_timeout_constant_exists():
    from pycastle.defaults.config import IDLE_TIMEOUT

    assert IDLE_TIMEOUT == 300


# ── Cycle 23-2: create_worktree and remove_worktree raise WorktreeTimeoutError on timeout ──


def test_create_worktree_raises_worktree_timeout_error(tmp_path):
    mock_svc = MagicMock(spec=GitService)
    mock_svc.verify_ref_exists.side_effect = GitTimeoutError("timed out")

    with pytest.raises(WorktreeTimeoutError):
        create_worktree(
            tmp_path, tmp_path / "wt", "feature/timeout", git_service=mock_svc
        )


def test_remove_worktree_raises_worktree_timeout_error(tmp_path):
    mock_svc = MagicMock(spec=GitService)
    mock_svc.remove_worktree.side_effect = GitTimeoutError("timed out")

    with pytest.raises(WorktreeTimeoutError):
        remove_worktree(tmp_path, tmp_path / "wt", git_service=mock_svc)


# ── Cycle 23-3: create_worktree and remove_worktree delegate to GitService ──


def test_create_worktree_creates_worktree_directory(repo, tmp_path):
    worktree = tmp_path / "wt"
    create_worktree(repo, worktree, "feature/new")
    assert worktree.exists()


def test_remove_worktree_removes_worktree_directory(repo, tmp_path):
    worktree = tmp_path / "wt"
    create_worktree(repo, worktree, "feature/remove-delegate")
    assert worktree.exists()
    remove_worktree(repo, worktree)
    assert not worktree.exists()


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def repo(git_repo):
    """git_repo with pyproject.toml committed so create_worktree validation passes."""
    (git_repo / "pyproject.toml").write_text("[project]\nname = 'test'\n")
    subprocess.run(
        ["git", "-C", str(git_repo), "add", "pyproject.toml"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(git_repo), "commit", "-m", "add pyproject"],
        check=True,
        capture_output=True,
    )
    return git_repo


# ── create_worktree: new branch ───────────────────────────────────────────────


def test_create_worktree_creates_new_branch(repo, tmp_path):
    worktree = tmp_path / "wt"
    create_worktree(repo, worktree, "feature/new")

    assert (worktree / "pyproject.toml").exists()
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "feature/new"],
        capture_output=True,
        text=True,
    ).stdout
    assert "feature/new" in branches


def test_create_worktree_existing_branch_checks_out_files(repo, tmp_path):
    subprocess.run(
        ["git", "-C", str(repo), "branch", "existing-branch"],
        check=True,
        capture_output=True,
    )
    worktree = tmp_path / "wt"
    create_worktree(repo, worktree, "existing-branch")

    assert (worktree / "pyproject.toml").exists()


# ── create_worktree: stale registration recovery ─────────────────────────────


def test_create_worktree_succeeds_after_stale_registration(repo, tmp_path):
    """Pruning must clear stale entries so a new worktree on a fresh path succeeds."""
    stale = tmp_path / "stale-wt"
    create_worktree(repo, stale, "feature/stale")
    # Delete directory without git worktree remove → stale registration
    shutil.rmtree(str(stale))

    fresh = tmp_path / "fresh-wt"
    create_worktree(repo, fresh, "feature/fresh")
    assert (fresh / "pyproject.toml").exists()


# ── create_worktree: error conditions ────────────────────────────────────────


def test_create_worktree_raises_on_git_failure(repo, tmp_path):
    """git won't check out the same branch in two worktrees; must raise RuntimeError."""
    create_worktree(repo, tmp_path / "wt1", "feature/same")
    with pytest.raises(RuntimeError, match="(?i)worktree add failed"):
        create_worktree(repo, tmp_path / "wt2", "feature/same")


def test_create_worktree_raises_when_project_files_missing(git_repo, tmp_path):
    """A worktree with no pyproject.toml or requirements.txt must raise."""
    worktree = tmp_path / "wt"
    with pytest.raises(RuntimeError, match="(?i)commit"):
        create_worktree(git_repo, worktree, "feature/no-project")


def test_create_worktree_error_includes_path_and_listing(git_repo, tmp_path):
    """The missing-files error must name the worktree path and list its contents."""
    worktree = tmp_path / "wt"
    with pytest.raises(RuntimeError) as exc_info:
        create_worktree(git_repo, worktree, "feature/no-project")

    msg = str(exc_info.value)
    assert str(worktree) in msg, f"worktree path missing from error: {msg!r}"
    assert "README.md" in msg, f"directory listing missing from error: {msg!r}"


# ── create_worktree: stale ancestor branch auto-recreation ───────────────────


def test_create_worktree_does_not_recreate_valid_ancestor_branch(git_repo, tmp_path):
    """An ancestor branch that already has project files must not be recreated from HEAD."""
    (git_repo / "pyproject.toml").write_text("[project]\nname = 'test'\n")
    subprocess.run(
        ["git", "-C", str(git_repo), "add", "pyproject.toml"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(git_repo), "commit", "-m", "add project files"],
        check=True,
        capture_output=True,
    )

    # Branch created from this commit — it has pyproject.toml
    subprocess.run(
        ["git", "-C", str(git_repo), "branch", "issue/3-valid-ancestor"],
        check=True,
        capture_output=True,
    )

    # Advance main so the branch tip becomes an ancestor of HEAD
    (git_repo / "extra.txt").write_text("extra")
    subprocess.run(
        ["git", "-C", str(git_repo), "add", "extra.txt"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(git_repo), "commit", "-m", "extra commit"],
        check=True,
        capture_output=True,
    )

    branch_tip_before = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "issue/3-valid-ancestor"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    worktree = tmp_path / "wt"
    create_worktree(git_repo, worktree, "issue/3-valid-ancestor")

    assert (worktree / "pyproject.toml").exists()
    branch_tip_after = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "issue/3-valid-ancestor"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert branch_tip_before == branch_tip_after, (
        "branch must not be recreated when project files are present"
    )


def test_create_worktree_raises_when_non_ancestor_branch_has_no_project_files(
    git_repo, tmp_path
):
    """A branch with real implementer commits that lacks project files must still raise."""
    # Create implementer branch and add a real commit to it (via a temp worktree)
    subprocess.run(
        ["git", "-C", str(git_repo), "branch", "issue/2-real-work"],
        check=True,
        capture_output=True,
    )
    temp_wt = tmp_path / "temp-wt"
    subprocess.run(
        [
            "git",
            "-C",
            str(git_repo),
            "worktree",
            "add",
            str(temp_wt),
            "issue/2-real-work",
        ],
        check=True,
        capture_output=True,
    )
    (temp_wt / "implementer_work.txt").write_text("real work")
    subprocess.run(
        ["git", "-C", str(temp_wt), "add", "implementer_work.txt"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(temp_wt), "commit", "-m", "real implementer work"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(git_repo), "worktree", "remove", str(temp_wt)],
        check=True,
        capture_output=True,
    )

    # Advance main independently so branch tip is NOT an ancestor of HEAD
    (git_repo / "pyproject.toml").write_text("[project]\nname = 'test'\n")
    subprocess.run(
        ["git", "-C", str(git_repo), "add", "pyproject.toml"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(git_repo), "commit", "-m", "add project files"],
        check=True,
        capture_output=True,
    )

    # Real work on branch, but no pyproject.toml — must raise, not silently discard
    worktree = tmp_path / "wt"
    with pytest.raises(RuntimeError, match="(?i)commit"):
        create_worktree(git_repo, worktree, "issue/2-real-work")


def test_create_worktree_recreates_stale_ancestor_branch(git_repo, tmp_path):
    """A branch created before pyproject.toml is auto-recreated from HEAD when stale."""
    # Branch is created when repo only has README.md (no project files)
    subprocess.run(
        ["git", "-C", str(git_repo), "branch", "issue/1-stale"],
        check=True,
        capture_output=True,
    )
    # Advance main: add pyproject.toml
    (git_repo / "pyproject.toml").write_text("[project]\nname = 'test'\n")
    subprocess.run(
        ["git", "-C", str(git_repo), "add", "pyproject.toml"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(git_repo), "commit", "-m", "add project files"],
        check=True,
        capture_output=True,
    )

    # Branch tip is now an ancestor of HEAD and has no project files — must succeed
    worktree = tmp_path / "wt"
    create_worktree(git_repo, worktree, "issue/1-stale")

    assert (worktree / "pyproject.toml").exists()


# ── remove_worktree ───────────────────────────────────────────────────────────


def test_remove_worktree_removes_directory(repo, tmp_path):
    worktree = tmp_path / "wt"
    create_worktree(repo, worktree, "feature/to-remove")
    assert worktree.exists()

    remove_worktree(repo, worktree)
    assert not worktree.exists()


def test_remove_worktree_is_silent_when_path_missing(repo, tmp_path):
    """remove_worktree must not raise if the directory was already deleted."""
    remove_worktree(repo, tmp_path / "nonexistent")


# ── Cycle D: .git file is patched to Linux gitdir path on Windows ─────────────


def test_patch_gitdir_rewrites_windows_path(tmp_path):
    """On Windows the function returns a temp file with the container-internal gitdir path."""
    worktree = tmp_path / "my-branch"
    worktree.mkdir()
    git_file = worktree / ".git"
    git_file.write_text("gitdir: C:/Users/johan/repo/.git/worktrees/my-branch\n")

    with patch("sys.platform", "win32"):
        result = patch_gitdir_for_container(worktree)

    assert result is not None
    assert (
        result.read_text().strip()
        == "gitdir: /.pycastle-parent-git/worktrees/my-branch"
    )
    assert (
        git_file.read_text() == "gitdir: C:/Users/johan/repo/.git/worktrees/my-branch\n"
    )


def test_create_worktree_recovers_from_stale_directory(repo, tmp_path):
    """A pre-existing directory at worktree_path must not permanently block create_worktree."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "leftover.txt").write_text("stale content from failed previous run")

    create_worktree(repo, worktree, "feature/recovery")
    assert (worktree / "pyproject.toml").exists()


def test_remove_worktree_falls_back_to_rmtree_when_git_fails(repo, tmp_path):
    """When git worktree remove exits non-zero the directory must still be removed.

    The rmtree fallback is implemented in GitService.remove_worktree; this test
    verifies the end-to-end behavior using a real repo and patching subprocess.
    """
    worktree = tmp_path / "wt"
    create_worktree(repo, worktree, "feature/fallback")
    assert worktree.exists()

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr=b"error")
        remove_worktree(repo, worktree)

    assert not worktree.exists()


# ── Cycle 32-1: mount overlay — patch writes temp file, never touches host ──


def test_patch_gitdir_returns_temp_file_and_leaves_host_unchanged(tmp_path):
    """Even when the host .git is locked (read-only), the function returns a
    temp file with corrected content and does not write to the host file."""
    worktree = tmp_path / "my-branch"
    worktree.mkdir()
    git_file = worktree / ".git"
    git_file.write_text("gitdir: C:/Users/johan/repo/.git/worktrees/my-branch\n")
    git_file.chmod(0o444)  # simulate exclusive lock — writes would fail

    with patch("sys.platform", "win32"):
        overlay = patch_gitdir_for_container(worktree)

    assert overlay is not None
    assert (
        overlay.read_text().strip()
        == "gitdir: /.pycastle-parent-git/worktrees/my-branch"
    )
    assert (
        git_file.read_text() == "gitdir: C:/Users/johan/repo/.git/worktrees/my-branch\n"
    )


def test_patch_gitdir_noop_on_non_windows(tmp_path):
    """On Linux/macOS the function returns None and leaves the .git file untouched."""
    worktree = tmp_path / "my-branch"
    worktree.mkdir()
    original = "gitdir: /home/user/repo/.git/worktrees/my-branch\n"
    (worktree / ".git").write_text(original)

    with patch("sys.platform", "linux"):
        result = patch_gitdir_for_container(worktree)

    assert result is None
    assert (worktree / ".git").read_text() == original
