import asyncio
import contextlib
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pycastle.config import Config
from pycastle.errors import WorktreeError, WorktreeTimeoutError
from pycastle.services import GitCommandError, GitService, GitTimeoutError
from pycastle.errors import HardAgentError, TransientAgentError, UsageLimitError
from pycastle.infrastructure.worktree import (
    transient_worktree,
    managed_worktree,
    patch_gitdir_for_container,
    worktree_name_for_branch,
    worktree_path,
)


# ── Cycle 23-1: timeout constants ────────────────────────────────────────────


def test_worktree_timeout_default_value():
    from pycastle.config import Config

    assert Config().worktree_timeout == 30


def test_idle_timeout_default_value():
    from pycastle.config import Config

    assert Config().idle_timeout == 300


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def repo(git_repo):
    """git_repo with pyproject.toml committed so worktree validation passes."""
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


@pytest.fixture
def real_branch_deps(repo):
    """Real deps backed by a repo that has pyproject.toml."""
    cfg = Config(pycastle_dir=".pycastle")
    return SimpleNamespace(repo_root=repo, cfg=cfg, git_svc=GitService(cfg))


@pytest.fixture
def bare_branch_deps(git_repo):
    """Real deps backed by a repo with no pyproject.toml — for error cases."""
    cfg = Config(pycastle_dir=".pycastle")
    return SimpleNamespace(repo_root=git_repo, cfg=cfg, git_svc=GitService(cfg))


@pytest.fixture
def branch_deps(tmp_path):
    """Mock-based deps for fast unit tests."""
    mock_svc = MagicMock(spec=GitService)
    cfg = Config(pycastle_dir=".pycastle")

    def _fake_create(repo, wt, branch, sha=None):
        wt.mkdir(parents=True, exist_ok=True)
        (wt / "pyproject.toml").write_text("[project]\nname='t'\n")

    mock_svc.verify_ref_exists.return_value = False
    mock_svc.list_worktrees.return_value = []
    mock_svc.create_worktree.side_effect = _fake_create
    return SimpleNamespace(repo_root=tmp_path, cfg=cfg, git_svc=mock_svc)


# ── managed_worktree: timeout and git errors ──────────────────────────────────


def test_managed_worktree_raises_worktree_timeout_error_when_git_times_out(branch_deps):
    branch_deps.git_svc.verify_ref_exists.side_effect = GitTimeoutError("timed out")

    async def _run():
        with pytest.raises(WorktreeTimeoutError):
            async with managed_worktree(
                "issue-42",
                branch="pycastle/issue-42",
                sha="abc123",
                delete_branch_on_teardown=True,
                deps=branch_deps,
            ):
                pass

    asyncio.run(_run())


def test_managed_worktree_raises_worktree_error_on_git_command_failure(branch_deps):
    branch_deps.git_svc.create_worktree.side_effect = GitCommandError("git died")

    async def _run():
        with pytest.raises(WorktreeError, match="git died"):
            async with managed_worktree(
                "issue-42",
                branch="pycastle/issue-42",
                sha="abc123",
                delete_branch_on_teardown=True,
                deps=branch_deps,
            ):
                pass

    asyncio.run(_run())


def test_managed_worktree_raises_when_registered_worktree_has_no_project_files(
    branch_deps,
):
    """A registered worktree with no project files must raise WorktreeError."""
    wt_path = branch_deps.repo_root / ".pycastle" / ".worktrees" / "issue-42"
    wt_path.mkdir(parents=True)
    branch_deps.git_svc.verify_ref_exists.return_value = True
    branch_deps.git_svc.list_worktrees.return_value = [wt_path]

    async def _run():
        with pytest.raises(WorktreeError, match="(?i)commit"):
            async with managed_worktree(
                "issue-42",
                branch="pycastle/issue-42",
                sha="abc123",
                delete_branch_on_teardown=True,
                deps=branch_deps,
            ):
                pass

    asyncio.run(_run())


# ── managed_worktree: integration tests against a real git repo ───────────────


def test_managed_worktree_yields_valid_path_in_real_repo(real_branch_deps):
    async def _run():
        async with managed_worktree(
            "issue-42",
            branch="pycastle/issue-42",
            sha=None,
            delete_branch_on_teardown=True,
            deps=real_branch_deps,
        ) as path:
            assert path.exists()
            assert (path / "pyproject.toml").exists()

    asyncio.run(_run())


def test_managed_worktree_creates_new_branch_in_repo(real_branch_deps):
    async def _run():
        async with managed_worktree(
            "issue-42",
            branch="pycastle/issue-42",
            sha=None,
            delete_branch_on_teardown=True,
            deps=real_branch_deps,
        ):
            branches = subprocess.run(
                [
                    "git",
                    "-C",
                    str(real_branch_deps.repo_root),
                    "branch",
                    "--list",
                    "pycastle/issue-42",
                ],
                capture_output=True,
                text=True,
            ).stdout
            assert "pycastle/issue-42" in branches

    asyncio.run(_run())


def test_managed_worktree_preserves_when_stage_done_sentinel_present(real_branch_deps):
    """Empty role dir (stage-done sentinel) must keep worktree and branch alive across teardown."""
    captured: dict = {}

    async def _run():
        async with managed_worktree(
            "issue-stage-done",
            branch="pycastle/issue-stage-done",
            sha=None,
            delete_branch_on_teardown=True,
            deps=real_branch_deps,
        ) as path:
            (path / ".pycastle-session" / "implementer").mkdir(parents=True)
            captured["path"] = path

    asyncio.run(_run())

    assert captured["path"].exists()
    branches = subprocess.run(
        [
            "git",
            "-C",
            str(real_branch_deps.repo_root),
            "branch",
            "--list",
            "pycastle/issue-stage-done",
        ],
        capture_output=True,
        text=True,
    ).stdout
    assert "pycastle/issue-stage-done" in branches


def test_managed_worktree_tears_down_when_no_role_dirs_and_clean_tree(real_branch_deps):
    """Clean tree with no `.pycastle-session/` must tear down both worktree and branch."""
    captured: dict = {}

    async def _run():
        async with managed_worktree(
            "issue-clean",
            branch="pycastle/issue-clean",
            sha=None,
            delete_branch_on_teardown=True,
            deps=real_branch_deps,
        ) as path:
            captured["path"] = path

    asyncio.run(_run())

    assert not captured["path"].exists()
    branches = subprocess.run(
        [
            "git",
            "-C",
            str(real_branch_deps.repo_root),
            "branch",
            "--list",
            "pycastle/issue-clean",
        ],
        capture_output=True,
        text=True,
    ).stdout
    assert "pycastle/issue-clean" not in branches


def test_managed_worktree_preserves_when_resumable_session_present(real_branch_deps):
    """Non-empty role dir (resumable session) must keep worktree and branch alive across teardown."""
    captured: dict = {}

    async def _run():
        async with managed_worktree(
            "issue-resumable",
            branch="pycastle/issue-resumable",
            sha=None,
            delete_branch_on_teardown=True,
            deps=real_branch_deps,
        ) as path:
            role_dir = path / ".pycastle-session" / "reviewer"
            role_dir.mkdir(parents=True)
            (role_dir / "session.jsonl").write_text("{}\n")
            captured["path"] = path

    asyncio.run(_run())

    assert captured["path"].exists()
    branches = subprocess.run(
        [
            "git",
            "-C",
            str(real_branch_deps.repo_root),
            "branch",
            "--list",
            "pycastle/issue-resumable",
        ],
        capture_output=True,
        text=True,
    ).stdout
    assert "pycastle/issue-resumable" in branches


def test_managed_worktree_with_existing_branch(real_branch_deps):
    """Entering managed_worktree for a branch that already exists must succeed."""
    subprocess.run(
        ["git", "-C", str(real_branch_deps.repo_root), "branch", "existing-branch"],
        check=True,
        capture_output=True,
    )

    async def _run():
        async with managed_worktree(
            "existing",
            branch="existing-branch",
            sha=None,
            delete_branch_on_teardown=False,
            deps=real_branch_deps,
        ) as path:
            assert (path / "pyproject.toml").exists()

    asyncio.run(_run())


def test_managed_worktree_succeeds_after_stale_git_registration(real_branch_deps):
    """A stale worktree entry (dir removed without git) must not block a fresh create."""
    repo = real_branch_deps.repo_root
    stale = repo / ".pycastle" / ".worktrees" / "stale"

    async def _create_stale():
        async with managed_worktree(
            "stale",
            branch="pycastle/stale",
            sha=None,
            delete_branch_on_teardown=False,
            deps=real_branch_deps,
        ):
            pass

    asyncio.run(_create_stale())

    # Re-add without git — simulate leftover registration
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", str(stale), "pycastle/stale"],
        check=True,
        capture_output=True,
    )
    shutil.rmtree(str(stale))

    async def _run():
        async with managed_worktree(
            "fresh",
            branch="pycastle/fresh",
            sha=None,
            delete_branch_on_teardown=True,
            deps=real_branch_deps,
        ) as path:
            assert (path / "pyproject.toml").exists()

    asyncio.run(_run())


def test_managed_worktree_raises_on_same_branch_conflict(real_branch_deps):
    """git won't check out the same branch in two worktrees — must raise WorktreeError."""

    async def _run():
        async with managed_worktree(
            "name1",
            branch="feature/same",
            sha=None,
            delete_branch_on_teardown=False,
            deps=real_branch_deps,
        ):
            with pytest.raises(WorktreeError, match="(?i)worktree add failed"):
                async with managed_worktree(
                    "name2",
                    branch="feature/same",
                    sha=None,
                    delete_branch_on_teardown=True,
                    deps=real_branch_deps,
                ):
                    pass

    asyncio.run(_run())


def test_managed_worktree_raises_when_project_files_missing(bare_branch_deps):
    """A repo with no pyproject.toml must cause managed_worktree to raise."""

    async def _run():
        with pytest.raises(WorktreeError, match="(?i)commit"):
            async with managed_worktree(
                "issue-42",
                branch="pycastle/issue-42",
                sha=None,
                delete_branch_on_teardown=True,
                deps=bare_branch_deps,
            ):
                pass

    asyncio.run(_run())


def test_managed_worktree_error_includes_path_and_listing(bare_branch_deps):
    """The missing-files error must name the worktree path and list directory contents."""

    async def _run():
        with pytest.raises(WorktreeError) as exc_info:
            async with managed_worktree(
                "issue-42",
                branch="pycastle/issue-42",
                sha=None,
                delete_branch_on_teardown=True,
                deps=bare_branch_deps,
            ):
                pass

        msg = str(exc_info.value)
        expected_path = worktree_path("issue-42", bare_branch_deps)
        assert str(expected_path) in msg, f"worktree path missing from error: {msg!r}"
        assert "README.md" in msg, f"directory listing missing from error: {msg!r}"

    asyncio.run(_run())


def test_managed_worktree_does_not_recreate_valid_ancestor_branch(git_repo):
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
    subprocess.run(
        ["git", "-C", str(git_repo), "branch", "issue/3-valid-ancestor"],
        check=True,
        capture_output=True,
    )
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

    cfg = Config(pycastle_dir=".pycastle")
    deps = SimpleNamespace(repo_root=git_repo, cfg=cfg, git_svc=GitService(cfg))

    async def _run():
        async with managed_worktree(
            "issue-3",
            branch="issue/3-valid-ancestor",
            sha=None,
            delete_branch_on_teardown=False,
            deps=deps,
        ) as path:
            assert (path / "pyproject.toml").exists()

    asyncio.run(_run())

    branch_tip_after = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "issue/3-valid-ancestor"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert branch_tip_before == branch_tip_after, (
        "branch must not be recreated when project files are present"
    )


def test_managed_worktree_raises_when_non_ancestor_branch_has_no_project_files(
    git_repo,
):
    """A branch with real commits but no project files must raise, not silently discard work."""
    subprocess.run(
        ["git", "-C", str(git_repo), "branch", "issue/2-real-work"],
        check=True,
        capture_output=True,
    )
    temp_wt = git_repo.parent / "temp-wt"
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

    cfg = Config(pycastle_dir=".pycastle")
    deps = SimpleNamespace(repo_root=git_repo, cfg=cfg, git_svc=GitService(cfg))

    async def _run():
        with pytest.raises(WorktreeError, match="(?i)commit"):
            async with managed_worktree(
                "issue-2",
                branch="issue/2-real-work",
                sha=None,
                delete_branch_on_teardown=True,
                deps=deps,
            ):
                pass

    asyncio.run(_run())


def test_managed_worktree_recreates_stale_ancestor_branch(git_repo):
    """A branch created before pyproject.toml is auto-recreated from HEAD when stale."""
    subprocess.run(
        ["git", "-C", str(git_repo), "branch", "issue/1-stale"],
        check=True,
        capture_output=True,
    )
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

    cfg = Config(pycastle_dir=".pycastle")
    deps = SimpleNamespace(repo_root=git_repo, cfg=cfg, git_svc=GitService(cfg))

    async def _run():
        async with managed_worktree(
            "issue-1",
            branch="issue/1-stale",
            sha=None,
            delete_branch_on_teardown=True,
            deps=deps,
        ) as path:
            assert (path / "pyproject.toml").exists()

    asyncio.run(_run())


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


def test_patch_gitdir_rewrites_linux_path(tmp_path):
    """On Linux the function returns a temp file with the container-internal gitdir path."""
    worktree = tmp_path / "my-branch"
    worktree.mkdir()
    git_file = worktree / ".git"
    git_file.write_text("gitdir: /home/user/repo/.git/worktrees/my-branch\n")

    with patch("sys.platform", "linux"):
        result = patch_gitdir_for_container(worktree)

    assert result is not None
    assert (
        result.read_text().strip()
        == "gitdir: /.pycastle-parent-git/worktrees/my-branch"
    )
    assert git_file.read_text() == "gitdir: /home/user/repo/.git/worktrees/my-branch\n"


# ── worktree_name_for_branch ──────────────────────────────────────────────────


def test_worktree_name_for_branch_extracts_issue_number_from_slug():
    assert worktree_name_for_branch("pycastle/issue-42-fix-the-bug") == "issue-42"


def test_worktree_name_for_branch_extracts_issue_number_without_slug():
    assert worktree_name_for_branch("pycastle/issue-7") == "issue-7"


def test_worktree_name_for_branch_falls_back_to_sanitised_slug():
    assert (
        worktree_name_for_branch("feature/my-cool-branch") == "feature-my-cool-branch"
    )


def test_worktree_name_for_branch_sanitises_special_chars():
    assert worktree_name_for_branch("UPPER/Case_Branch!") == "upper-case-branch"


def test_worktree_name_for_branch_extracts_issue_zero():
    assert worktree_name_for_branch("pycastle/issue-0") == "issue-0"


def test_worktree_name_for_branch_does_not_match_issue_number_in_non_pycastle_branch():
    # re.match anchors at the start: only pycastle/issue-N branches get the
    # issue-N shortname; other branches containing issue-N fall back to the
    # sanitised slug.
    assert worktree_name_for_branch("feature/issue-5-work") == "feature-issue-5-work"


# ── worktree_path ─────────────────────────────────────────────────────────────


def test_worktree_path_constructs_correct_path(tmp_path):
    cfg = Config(pycastle_dir=".pycastle")
    deps = SimpleNamespace(repo_root=tmp_path, cfg=cfg)
    result = worktree_path("issue-42", deps)
    assert result == tmp_path / ".pycastle" / ".worktrees" / "issue-42"


def test_worktree_path_respects_configured_pycastle_dir(tmp_path):
    cfg = Config(pycastle_dir="custom-dir")
    deps = SimpleNamespace(repo_root=tmp_path, cfg=cfg)
    result = worktree_path("issue-99", deps)
    assert result == tmp_path / "custom-dir" / ".worktrees" / "issue-99"


# ── transient_worktree ────────────────────────────────────────────────────────


@pytest.fixture
def detached_deps(tmp_path):
    mock_svc = MagicMock(spec=GitService)
    cfg = Config(pycastle_dir=".pycastle")
    return SimpleNamespace(repo_root=tmp_path, cfg=cfg, git_svc=mock_svc)


def test_transient_worktree_creates_worktree_on_enter(detached_deps):
    expected_path = detached_deps.repo_root / ".pycastle" / ".worktrees" / "sandbox"

    async def _run():
        async with transient_worktree("sandbox", sha="abc123", deps=detached_deps):
            detached_deps.git_svc.checkout_detached.assert_called_once_with(
                detached_deps.repo_root, expected_path, "abc123"
            )

    asyncio.run(_run())


def test_transient_worktree_yields_correct_path(detached_deps):
    expected_path = detached_deps.repo_root / ".pycastle" / ".worktrees" / "sandbox"

    async def _run():
        async with transient_worktree(
            "sandbox", sha="abc123", deps=detached_deps
        ) as path:
            assert path == expected_path

    asyncio.run(_run())


def test_transient_worktree_removes_worktree_on_clean_exit(detached_deps):
    expected_path = detached_deps.repo_root / ".pycastle" / ".worktrees" / "sandbox"

    async def _run():
        async with transient_worktree("sandbox", sha="abc123", deps=detached_deps):
            pass

    asyncio.run(_run())
    detached_deps.git_svc.remove_worktree.assert_called_once_with(
        detached_deps.repo_root, expected_path
    )


def test_transient_worktree_removes_worktree_when_body_raises(detached_deps):
    expected_path = detached_deps.repo_root / ".pycastle" / ".worktrees" / "sandbox"

    async def _run():
        with pytest.raises(RuntimeError, match="body error"):
            async with transient_worktree("sandbox", sha="abc123", deps=detached_deps):
                raise RuntimeError("body error")

    asyncio.run(_run())
    detached_deps.git_svc.remove_worktree.assert_called_once_with(
        detached_deps.repo_root, expected_path
    )


def test_transient_worktree_removes_worktree_on_usage_limit_error(detached_deps):
    """transient_worktree must always tear down — even on UsageLimitError."""
    expected_path = detached_deps.repo_root / ".pycastle" / ".worktrees" / "sandbox"

    async def _run():
        with pytest.raises(UsageLimitError):
            async with transient_worktree("sandbox", sha="abc123", deps=detached_deps):
                raise UsageLimitError(reset_time=None)

    asyncio.run(_run())
    detached_deps.git_svc.remove_worktree.assert_called_once_with(
        detached_deps.repo_root, expected_path
    )


def test_transient_worktree_does_not_remove_worktree_when_checkout_fails(detached_deps):
    detached_deps.git_svc.checkout_detached.side_effect = RuntimeError(
        "checkout failed"
    )

    async def _run():
        with pytest.raises(RuntimeError, match="checkout failed"):
            async with transient_worktree("sandbox", sha="abc123", deps=detached_deps):
                pass

    asyncio.run(_run())
    detached_deps.git_svc.remove_worktree.assert_not_called()


def test_transient_worktree_propagates_cleanup_error(detached_deps):
    """An error from remove_worktree in the finally block must propagate."""
    detached_deps.git_svc.remove_worktree.side_effect = RuntimeError("disk full")

    async def _run():
        with pytest.raises(RuntimeError, match="disk full"):
            async with transient_worktree("sandbox", sha="abc123", deps=detached_deps):
                pass

    asyncio.run(_run())


# ── managed_worktree ──────────────────────────────────────────────────────────


def test_managed_worktree_creates_worktree_on_enter_and_yields_correct_path(
    branch_deps,
):
    expected_path = branch_deps.repo_root / ".pycastle" / ".worktrees" / "issue-42"

    async def _run():
        async with managed_worktree(
            "issue-42",
            branch="pycastle/issue-42",
            sha="abc123",
            delete_branch_on_teardown=True,
            deps=branch_deps,
        ) as path:
            assert path == expected_path
            assert expected_path.exists()

    asyncio.run(_run())


def test_managed_worktree_removes_worktree_and_branch_on_clean_exit(branch_deps):
    expected_path = branch_deps.repo_root / ".pycastle" / ".worktrees" / "issue-42"

    async def _run():
        async with managed_worktree(
            "issue-42",
            branch="pycastle/issue-42",
            sha="abc123",
            delete_branch_on_teardown=True,
            deps=branch_deps,
        ):
            pass

    asyncio.run(_run())
    branch_deps.git_svc.remove_worktree.assert_called_once_with(
        branch_deps.repo_root, expected_path
    )
    branch_deps.git_svc.delete_branch.assert_called_once_with(
        "pycastle/issue-42", branch_deps.repo_root
    )


def test_managed_worktree_removes_worktree_but_not_branch_when_delete_branch_on_teardown_false(
    branch_deps,
):
    async def _run():
        async with managed_worktree(
            "issue-42",
            branch="pycastle/issue-42",
            sha="abc123",
            delete_branch_on_teardown=False,
            deps=branch_deps,
        ):
            pass

    asyncio.run(_run())
    branch_deps.git_svc.remove_worktree.assert_called_once()
    branch_deps.git_svc.delete_branch.assert_not_called()


def test_managed_worktree_cleans_up_on_exception(branch_deps):
    expected_path = branch_deps.repo_root / ".pycastle" / ".worktrees" / "issue-42"

    async def _run():
        with pytest.raises(RuntimeError, match="body error"):
            async with managed_worktree(
                "issue-42",
                branch="pycastle/issue-42",
                sha="abc123",
                delete_branch_on_teardown=True,
                deps=branch_deps,
            ):
                raise RuntimeError("body error")

    asyncio.run(_run())
    branch_deps.git_svc.remove_worktree.assert_called_once_with(
        branch_deps.repo_root, expected_path
    )
    branch_deps.git_svc.delete_branch.assert_called_once_with(
        "pycastle/issue-42", branch_deps.repo_root
    )


def test_managed_worktree_does_not_delete_branch_when_remove_worktree_raises(
    branch_deps,
):
    """teardown_worktree and delete_branch are gated together — if teardown fails, delete does not fire."""
    branch_deps.git_svc.remove_worktree.side_effect = RuntimeError("disk full")

    async def _run():
        with pytest.raises(RuntimeError, match="disk full"):
            async with managed_worktree(
                "issue-42",
                branch="pycastle/issue-42",
                sha="abc123",
                delete_branch_on_teardown=True,
                deps=branch_deps,
            ):
                pass

    asyncio.run(_run())
    branch_deps.git_svc.delete_branch.assert_not_called()


def test_managed_worktree_does_not_run_cleanup_when_create_fails(branch_deps):
    from pycastle.errors import WorktreeError

    branch_deps.git_svc.create_worktree.side_effect = WorktreeError("create failed")

    async def _run():
        with pytest.raises(WorktreeError, match="create failed"):
            async with managed_worktree(
                "issue-42",
                branch="pycastle/issue-42",
                sha="abc123",
                delete_branch_on_teardown=True,
                deps=branch_deps,
            ):
                pass

    asyncio.run(_run())
    branch_deps.git_svc.remove_worktree.assert_not_called()
    branch_deps.git_svc.delete_branch.assert_not_called()


def test_managed_worktree_keeps_worktrees_dir_when_sibling_worktree_remains(
    real_branch_deps,
):
    worktrees_dir = real_branch_deps.repo_root / ".pycastle" / ".worktrees"

    async def _run():
        async with managed_worktree(
            "issue-10",
            branch="pycastle/issue-10",
            sha=None,
            delete_branch_on_teardown=False,
            deps=real_branch_deps,
        ):
            async with managed_worktree(
                "issue-11",
                branch="pycastle/issue-11",
                sha=None,
                delete_branch_on_teardown=True,
                deps=real_branch_deps,
            ):
                pass
            assert worktrees_dir.exists()

    asyncio.run(_run())


def test_managed_worktree_removes_worktrees_dir_when_last_worktree_exits(
    real_branch_deps,
):
    worktrees_dir = real_branch_deps.repo_root / ".pycastle" / ".worktrees"

    async def _run():
        async with managed_worktree(
            "issue-42",
            branch="pycastle/issue-42",
            sha=None,
            delete_branch_on_teardown=True,
            deps=real_branch_deps,
        ):
            assert worktrees_dir.exists()
        assert not worktrees_dir.exists()

    asyncio.run(_run())


def test_transient_worktree_removes_worktrees_dir_when_last_worktree_exits(
    real_branch_deps,
):
    worktrees_dir = real_branch_deps.repo_root / ".pycastle" / ".worktrees"
    sha = real_branch_deps.git_svc.get_head_sha(real_branch_deps.repo_root)

    async def _run():
        async with transient_worktree("sandbox", sha=sha, deps=real_branch_deps):
            assert worktrees_dir.exists()
        assert not worktrees_dir.exists()

    asyncio.run(_run())


# ── post-creation clean-status invariant ─────────────────────────────────────


def _set_autocrlf(repo_root, value):
    subprocess.run(
        ["git", "-C", str(repo_root), "config", "core.autocrlf", value],
        check=True,
        capture_output=True,
    )


def _assert_worktree_clean(path):
    result = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "", (
        f"dirty worktree after creation: {result.stdout!r}"
    )


@pytest.mark.parametrize("autocrlf", [None, "true"])
def test_managed_worktree_has_clean_status_after_creation(real_branch_deps, autocrlf):
    """managed_worktree must produce a clean working tree, even when host has core.autocrlf=true."""
    if autocrlf is not None:
        _set_autocrlf(real_branch_deps.repo_root, autocrlf)

    async def _run():
        async with managed_worktree(
            f"issue-eol-{autocrlf}",
            branch=f"pycastle/issue-eol-{autocrlf}",
            sha=None,
            delete_branch_on_teardown=True,
            deps=real_branch_deps,
        ) as path:
            _assert_worktree_clean(path)

    asyncio.run(_run())


@pytest.mark.parametrize("autocrlf", [None, "true"])
def test_transient_worktree_has_clean_status_after_creation(real_branch_deps, autocrlf):
    """transient_worktree must produce a clean working tree, even when host has core.autocrlf=true."""
    if autocrlf is not None:
        _set_autocrlf(real_branch_deps.repo_root, autocrlf)
    sha = real_branch_deps.git_svc.get_head_sha(real_branch_deps.repo_root)

    async def _run():
        async with transient_worktree(
            f"sandbox-eol-{autocrlf}", sha=sha, deps=real_branch_deps
        ) as path:
            _assert_worktree_clean(path)

    asyncio.run(_run())


# ── managed_worktree: broadened preservation rule ────────────────────────────


def test_managed_worktree_preserves_worktree_and_branch_when_session_dir_has_files(
    branch_deps,
):
    """managed_worktree must not teardown when a role session dir is non-empty."""

    async def _run():
        async with managed_worktree(
            "merge-sandbox",
            branch="pycastle/merge-sandbox",
            sha=None,
            delete_branch_on_teardown=True,
            deps=branch_deps,
        ) as wt_path:
            session_dir = wt_path / ".pycastle-session" / "merger"
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "session.json").write_text("{}")

    asyncio.run(_run())

    branch_deps.git_svc.remove_worktree.assert_not_called()
    branch_deps.git_svc.delete_branch.assert_not_called()


def test_managed_worktree_preserves_worktree_on_usage_limit_error(branch_deps):
    """managed_worktree must not teardown when UsageLimitError propagates from the body."""

    async def _run():
        with pytest.raises(UsageLimitError):
            async with managed_worktree(
                "issue-42",
                branch="pycastle/issue-42",
                sha=None,
                delete_branch_on_teardown=True,
                deps=branch_deps,
            ):
                raise UsageLimitError(reset_time=None)

    asyncio.run(_run())

    branch_deps.git_svc.remove_worktree.assert_not_called()
    branch_deps.git_svc.delete_branch.assert_not_called()


# ── managed_worktree: parametrised preservation predicate ────────────────────


@pytest.mark.parametrize(
    "dirty, usage_limit_exc, has_resumable_session, expected_teardown",
    [
        (False, False, False, True),
        (True, False, False, False),
        (False, True, False, False),
        (False, False, True, False),
        (True, True, False, False),
        (True, False, True, False),
        (False, True, True, False),
        (True, True, True, False),
    ],
)
def test_managed_worktree_preservation_predicate(
    branch_deps, dirty, usage_limit_exc, has_resumable_session, expected_teardown
):
    """preserve iff dirty OR usage_limit_exc OR has_resumable_session."""
    branch_deps.git_svc.is_working_tree_clean.return_value = not dirty

    async def _run():
        with contextlib.suppress(UsageLimitError):
            async with managed_worktree(
                "issue-42",
                branch="pycastle/issue-42",
                sha=None,
                delete_branch_on_teardown=True,
                deps=branch_deps,
            ) as wt_path:
                if has_resumable_session:
                    session_dir = wt_path / ".pycastle-session" / "implementer"
                    session_dir.mkdir(parents=True, exist_ok=True)
                    (session_dir / "session.json").write_text("{}")
                if usage_limit_exc:
                    raise UsageLimitError(reset_time=None)

    asyncio.run(_run())

    if expected_teardown:
        branch_deps.git_svc.remove_worktree.assert_called_once()
    else:
        branch_deps.git_svc.remove_worktree.assert_not_called()


def test_managed_worktree_preserves_worktree_on_transient_agent_error(branch_deps):
    """managed_worktree must not teardown when TransientAgentError propagates from the body."""

    async def _run():
        with pytest.raises(TransientAgentError):
            async with managed_worktree(
                "issue-42",
                branch="pycastle/issue-42",
                sha=None,
                delete_branch_on_teardown=True,
                deps=branch_deps,
            ):
                raise TransientAgentError(status_code=529)

    asyncio.run(_run())

    branch_deps.git_svc.remove_worktree.assert_not_called()
    branch_deps.git_svc.delete_branch.assert_not_called()


def test_managed_worktree_preserves_worktree_on_hard_agent_error(branch_deps):
    """managed_worktree must not teardown when HardAgentError propagates from the body."""

    async def _run():
        with pytest.raises(HardAgentError):
            async with managed_worktree(
                "issue-42",
                branch="pycastle/issue-42",
                sha=None,
                delete_branch_on_teardown=True,
                deps=branch_deps,
            ):
                raise HardAgentError(status_code=403)

    asyncio.run(_run())

    branch_deps.git_svc.remove_worktree.assert_not_called()
    branch_deps.git_svc.delete_branch.assert_not_called()


# ── prune_orphan_worktrees ────────────────────────────────────────────────────


def _make_prune_git_svc(active_paths: list[Path]) -> GitService:
    mock_svc = MagicMock(spec=GitService)
    mock_svc.list_worktrees.return_value = active_paths
    return mock_svc


def test_prune_orphan_worktrees_respects_custom_pycastle_dir(tmp_path):
    """With pycastle_dir='my-castle', orphans under my-castle/.worktrees/ are removed."""
    from pycastle.infrastructure.worktree import prune_orphan_worktrees

    cfg = Config(pycastle_dir="my-castle")
    worktrees_dir = tmp_path / "my-castle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)
    orphan = worktrees_dir / "orphan-branch"
    orphan.mkdir()

    prune_orphan_worktrees(tmp_path, cfg=cfg, git_service=_make_prune_git_svc([]))

    assert not orphan.exists()


def test_prune_orphan_worktrees_default_pycastle_dir_still_works(tmp_path):
    """With the default pycastle_dir='pycastle', orphans under pycastle/.worktrees/ are removed."""
    from pycastle.infrastructure.worktree import prune_orphan_worktrees

    cfg = Config(pycastle_dir="pycastle")
    worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)
    orphan = worktrees_dir / "orphan-branch"
    orphan.mkdir()

    prune_orphan_worktrees(tmp_path, cfg=cfg, git_service=_make_prune_git_svc([]))

    assert not orphan.exists()


def test_prune_orphan_worktrees_removes_worktrees_parent_when_empty_custom_dir(
    tmp_path,
):
    """Parent .worktrees dir is removed when empty after orphan sweep, custom pycastle_dir."""
    from pycastle.infrastructure.worktree import prune_orphan_worktrees

    cfg = Config(pycastle_dir="my-castle")
    worktrees_dir = tmp_path / "my-castle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)
    orphan = worktrees_dir / "orphan-branch"
    orphan.mkdir()

    prune_orphan_worktrees(tmp_path, cfg=cfg, git_service=_make_prune_git_svc([]))

    assert not worktrees_dir.exists()


def test_prune_orphan_worktrees_does_not_look_in_hardcoded_pycastle_dir(tmp_path):
    """When pycastle_dir='my-castle', orphans under hardcoded 'pycastle/.worktrees' are NOT swept."""
    from pycastle.infrastructure.worktree import prune_orphan_worktrees

    cfg = Config(pycastle_dir="my-castle")
    # put an orphan in the old hardcoded location
    old_worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    old_worktrees_dir.mkdir(parents=True)
    orphan_in_old_location = old_worktrees_dir / "orphan"
    orphan_in_old_location.mkdir()

    prune_orphan_worktrees(tmp_path, cfg=cfg, git_service=_make_prune_git_svc([]))

    # The function with custom dir must not touch the old location
    assert orphan_in_old_location.exists()
