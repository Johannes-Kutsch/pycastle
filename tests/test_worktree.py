import asyncio
import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from pycastle.errors import WorktreeTimeoutError
from pycastle.git_service import GitService, GitTimeoutError
from pycastle.worktree import (
    create_worktree,
    managed_worktree,
    patch_gitdir_for_container,
    remove_worktree,
)


# ── Cycle 23-1: timeout constants ────────────────────────────────────────────


def test_worktree_timeout_constant_exists():
    from pycastle.config import WORKTREE_TIMEOUT

    assert WORKTREE_TIMEOUT == 30


def test_idle_timeout_constant_exists():
    from pycastle.config import IDLE_TIMEOUT

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


# ── create_worktree: registered worktree reuse (issue #181) ──────────────────


def test_create_worktree_reuses_registered_worktree(tmp_path):
    """Path exists on disk AND is registered → svc.create_worktree not called."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "pyproject.toml").write_text("[project]\nname = 'test'\n")

    mock_svc = MagicMock(spec=GitService)
    mock_svc.verify_ref_exists.return_value = True
    mock_svc.list_worktrees.return_value = [worktree]

    create_worktree(tmp_path, worktree, "feature/reuse", git_service=mock_svc)

    mock_svc.create_worktree.assert_not_called()
    mock_svc.remove_worktree.assert_not_called()


def test_create_worktree_reuse_still_checks_has_files(tmp_path):
    """Registered worktree without project files still raises WorktreeError."""
    from pycastle.errors import WorktreeError

    worktree = tmp_path / "wt"
    worktree.mkdir()
    # No pyproject.toml or requirements.txt

    mock_svc = MagicMock(spec=GitService)
    mock_svc.verify_ref_exists.return_value = True
    mock_svc.list_worktrees.return_value = [worktree]
    mock_svc.is_ancestor.return_value = False

    with pytest.raises(WorktreeError, match="(?i)commit"):
        create_worktree(
            tmp_path, worktree, "feature/reuse-no-files", git_service=mock_svc
        )

    mock_svc.create_worktree.assert_not_called()


def test_create_worktree_removes_orphan_directory(tmp_path):
    """Path exists on disk but NOT registered → remove and recreate."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "leftover.txt").write_text("orphan")

    mock_svc = MagicMock(spec=GitService)
    mock_svc.verify_ref_exists.return_value = False
    mock_svc.list_worktrees.return_value = []

    def _fake_create(repo, wt, branch, sha=None):
        wt.mkdir(exist_ok=True)
        (wt / "pyproject.toml").write_text("[project]\nname='t'\n")

    mock_svc.create_worktree.side_effect = _fake_create

    create_worktree(tmp_path, worktree, "feature/orphan", git_service=mock_svc)

    mock_svc.remove_worktree.assert_called_once_with(tmp_path, worktree)
    mock_svc.create_worktree.assert_called_once()


def test_create_worktree_fresh_when_not_registered(tmp_path):
    """Path does not exist and not registered → fresh create_worktree called."""
    worktree = tmp_path / "wt"
    # worktree does NOT exist on disk

    mock_svc = MagicMock(spec=GitService)
    mock_svc.verify_ref_exists.return_value = False
    mock_svc.list_worktrees.return_value = []

    def _fake_create(repo, wt, branch, sha=None):
        wt.mkdir(exist_ok=True)
        (wt / "pyproject.toml").write_text("[project]\nname='t'\n")

    mock_svc.create_worktree.side_effect = _fake_create

    create_worktree(tmp_path, worktree, "feature/fresh", git_service=mock_svc)

    mock_svc.remove_worktree.assert_not_called()
    mock_svc.create_worktree.assert_called_once()


def test_create_worktree_git_command_error_raises_worktree_error(tmp_path):
    """GitCommandError from svc.create_worktree is wrapped as WorktreeError."""
    from pycastle.errors import WorktreeError
    from pycastle.git_service import GitCommandError

    worktree = tmp_path / "wt"

    mock_svc = MagicMock(spec=GitService)
    mock_svc.verify_ref_exists.return_value = False
    mock_svc.list_worktrees.return_value = []
    mock_svc.create_worktree.side_effect = GitCommandError("git died")

    with pytest.raises(WorktreeError, match="git died"):
        create_worktree(tmp_path, worktree, "feature/broken", git_service=mock_svc)


# ── Registered worktree without project files: no recreation attempt ─────────


def test_create_worktree_registered_no_files_raises_immediately(tmp_path):
    """Registered worktree without project files raises WorktreeError immediately.

    The stale-branch recreation path only applies to freshly-created worktrees.
    A worktree that is already registered is reported as broken rather than
    silently rebuilt, even when the branch would qualify as a stale ancestor.
    """
    from pycastle.errors import WorktreeError

    worktree = tmp_path / "wt"
    worktree.mkdir()

    mock_svc = MagicMock(spec=GitService)
    mock_svc.verify_ref_exists.return_value = True
    mock_svc.list_worktrees.return_value = [worktree]
    mock_svc.is_ancestor.return_value = True  # would qualify for recreation

    with pytest.raises(WorktreeError, match="(?i)commit"):
        create_worktree(
            tmp_path, worktree, "feature/registered-no-files", git_service=mock_svc
        )

    mock_svc.is_ancestor.assert_not_called()
    mock_svc.delete_branch.assert_not_called()
    mock_svc.create_worktree.assert_not_called()


# ── Issue 240 Fix 1: non-ancestor branch with no project files ───────────────


def test_create_worktree_raises_without_removing_when_non_ancestor_has_no_files(
    tmp_path,
):
    """WorktreeError raised immediately (no remove_worktree) when branch has unique commits but no project files."""
    from pycastle.errors import WorktreeError

    worktree = tmp_path / "wt"

    mock_svc = MagicMock(spec=GitService)
    mock_svc.verify_ref_exists.return_value = True
    mock_svc.list_worktrees.return_value = []

    def _fake_create(repo, wt, branch, sha=None):
        wt.mkdir(exist_ok=True)

    mock_svc.create_worktree.side_effect = _fake_create
    mock_svc.is_ancestor.return_value = False

    with pytest.raises(WorktreeError, match="(?i)unique commit"):
        create_worktree(
            tmp_path, worktree, "feature/unique-commits", git_service=mock_svc
        )

    mock_svc.remove_worktree.assert_not_called()


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


# ── Issue-229: managed_worktree context manager ───────────────────────────────


def _make_managed_worktree_mock_svc(tmp_path):
    """Return a mock GitService that creates files when create_worktree is called."""
    mock_svc = MagicMock(spec=GitService)
    mock_svc.verify_ref_exists.return_value = False
    mock_svc.list_worktrees.return_value = []

    def _fake_create(repo, wt, branch, sha=None):
        wt.mkdir(exist_ok=True)
        (wt / "pyproject.toml").write_text("[project]\nname='t'\n")

    mock_svc.create_worktree.side_effect = _fake_create
    return mock_svc


def test_managed_worktree_creates_worktree_on_enter(tmp_path):
    """managed_worktree must create the worktree directory before yielding."""
    wt_path = tmp_path / "wt"
    mock_svc = _make_managed_worktree_mock_svc(tmp_path)

    async def _run():
        async with managed_worktree(
            tmp_path, wt_path, "feature/test", git_service=mock_svc
        ):
            assert wt_path.exists()

    asyncio.run(_run())


def test_managed_worktree_removes_worktree_on_exit(tmp_path):
    """managed_worktree must call remove_worktree after the body exits (success path)."""
    wt_path = tmp_path / "wt"
    mock_svc = _make_managed_worktree_mock_svc(tmp_path)

    async def _run():
        async with managed_worktree(
            tmp_path, wt_path, "feature/test", git_service=mock_svc
        ):
            pass

    asyncio.run(_run())

    mock_svc.remove_worktree.assert_called_once_with(tmp_path, wt_path)


def test_managed_worktree_removes_worktree_on_exception(tmp_path):
    """managed_worktree must call remove_worktree even when the body raises."""
    wt_path = tmp_path / "wt"
    mock_svc = _make_managed_worktree_mock_svc(tmp_path)

    async def _run():
        with pytest.raises(RuntimeError, match="body error"):
            async with managed_worktree(
                tmp_path, wt_path, "feature/test", git_service=mock_svc
            ):
                raise RuntimeError("body error")

    asyncio.run(_run())

    mock_svc.remove_worktree.assert_called_once_with(tmp_path, wt_path)
