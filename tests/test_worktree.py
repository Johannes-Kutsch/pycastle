import asyncio
import contextlib
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pycastle.config import Config
from pycastle.errors import (
    AgentCredentialFailureError,
    AgentFailedError,
    HardAgentError,
    TransientAgentError,
    UsageLimitError,
    WorktreeError,
    WorktreeTimeoutError,
)
from pycastle.services import GitCommandError, GitService, GitTimeoutError
from pycastle.infrastructure.worktree import (
    durable_issue_worktree,
    managed_worktree,
    patch_gitdir_for_container,
    reusable_sandbox_worktree,
    transient_worktree,
    worktree_identity,
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
    cfg = Config()
    return SimpleNamespace(repo_root=repo, cfg=cfg, git_svc=GitService(cfg))


@pytest.fixture
def bare_branch_deps(git_repo):
    """Real deps backed by a repo with no pyproject.toml — for error cases."""
    cfg = Config()
    return SimpleNamespace(repo_root=git_repo, cfg=cfg, git_svc=GitService(cfg))


@pytest.fixture
def branch_deps(tmp_path):
    """Mock-based deps for fast unit tests."""
    mock_svc = MagicMock(spec=GitService)
    cfg = Config()

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
    wt_path = branch_deps.repo_root / "pycastle" / ".worktrees" / "issue-42"
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


def test_durable_issue_worktree_uses_existing_issue_path_layout(real_branch_deps):
    expected = worktree_identity("pycastle/issue-42", real_branch_deps.repo_root).path

    async def _run():
        async with durable_issue_worktree(42, sha=None, deps=real_branch_deps) as path:
            assert path == expected
            assert path.exists()
            assert (path / "pyproject.toml").exists()

    asyncio.run(_run())


def test_durable_issue_worktree_raises_worktree_timeout_error_when_git_times_out(
    branch_deps,
):
    branch_deps.git_svc.verify_ref_exists.side_effect = GitTimeoutError("timed out")

    async def _run():
        with pytest.raises(WorktreeTimeoutError):
            async with durable_issue_worktree(42, sha="abc123", deps=branch_deps):
                pass

    asyncio.run(_run())


def test_durable_issue_worktree_raises_worktree_error_on_git_command_failure(
    branch_deps,
):
    branch_deps.git_svc.create_worktree.side_effect = GitCommandError("git died")

    async def _run():
        with pytest.raises(WorktreeError, match="git died"):
            async with durable_issue_worktree(42, sha="abc123", deps=branch_deps):
                pass

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


def test_durable_issue_worktree_tears_down_empty_issue_branch_on_clean_exit(
    real_branch_deps,
):
    captured: dict = {}

    async def _run():
        async with durable_issue_worktree(42, sha=None, deps=real_branch_deps) as path:
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
            "pycastle/issue-42",
        ],
        capture_output=True,
        text=True,
    ).stdout
    assert "pycastle/issue-42" not in branches


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


def test_managed_worktree_preserves_clean_worktree_on_unexpected_agent_failure(
    real_branch_deps,
):
    """Unexpected agent failures must preserve a clean worktree for debugging."""
    captured: dict = {}

    async def _run():
        with pytest.raises(RuntimeError, match="unexpected crash"):
            async with managed_worktree(
                "issue-unexpected-failure",
                branch="pycastle/issue-unexpected-failure",
                sha=None,
                delete_branch_on_teardown=True,
                deps=real_branch_deps,
            ) as path:
                captured["path"] = path
                raise RuntimeError("unexpected crash")

    asyncio.run(_run())

    assert captured["path"].exists()
    assert (captured["path"] / ".pycastle-session" / ".preserved-failure").is_file()
    branches = subprocess.run(
        [
            "git",
            "-C",
            str(real_branch_deps.repo_root),
            "branch",
            "--list",
            "pycastle/issue-unexpected-failure",
        ],
        capture_output=True,
        text=True,
    ).stdout
    assert "pycastle/issue-unexpected-failure" in branches


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
    stale = repo / "pycastle" / ".worktrees" / "stale"

    async def _create_stale():
        async with managed_worktree(
            "stale",
            branch="pycastle/stale",
            sha=None,
            delete_branch_on_teardown=False,
            deps=real_branch_deps,
        ) as wt:
            # commit so the branch has WIP and is preserved by the empty-branch cleanup rule
            (wt / "stale.txt").write_text("wip")
            subprocess.run(
                ["git", "-C", str(wt), "add", "stale.txt"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(wt), "commit", "-m", "wip"],
                check=True,
                capture_output=True,
            )

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
        expected_path = worktree_identity(
            "pycastle/issue-42", bare_branch_deps.repo_root, name="issue-42"
        ).path
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

    cfg = Config()
    deps = SimpleNamespace(repo_root=git_repo, cfg=cfg, git_svc=GitService(cfg))

    main_tip = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    async def _run():
        async with managed_worktree(
            "issue-3",
            branch="issue/3-valid-ancestor",
            sha=None,
            delete_branch_on_teardown=False,
            deps=deps,
        ) as path:
            assert (path / "pyproject.toml").exists()
            assert not (path / "extra.txt").exists(), (
                "branch must not be recreated from HEAD"
            )
            # commit WIP so the branch is preserved after teardown
            (path / "impl.txt").write_text("impl")
            subprocess.run(
                ["git", "-C", str(path), "add", "impl.txt"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(path), "commit", "-m", "impl"],
                check=True,
                capture_output=True,
            )

    asyncio.run(_run())

    branch_tip_after = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "issue/3-valid-ancestor"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert branch_tip_after != main_tip, (
        "branch must not be recreated from HEAD (tip must diverge from main)"
    )
    assert branch_tip_after != branch_tip_before, (
        "new commit must advance the branch tip"
    )


def test_managed_worktree_raises_when_non_ancestor_branch_has_no_project_files(
    git_repo,
):
    """Non-ephemeral (delete_branch_on_teardown=False) branch with real commits but no project
    files must raise, not silently discard work."""
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

    cfg = Config()
    deps = SimpleNamespace(repo_root=git_repo, cfg=cfg, git_svc=GitService(cfg))

    async def _run():
        with pytest.raises(WorktreeError, match="(?i)commit"):
            async with managed_worktree(
                "issue-2",
                branch="issue/2-real-work",
                sha=None,
                delete_branch_on_teardown=False,
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

    cfg = Config()
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


def test_worktree_identity_uses_module_owned_issue_name_and_path(tmp_path):
    identity = worktree_identity("pycastle/issue-42-add-worktree-identity", tmp_path)

    assert identity.branch == "pycastle/issue-42-add-worktree-identity"
    assert identity.name == "issue-42"
    assert identity.path == tmp_path / "pycastle" / ".worktrees" / "issue-42"


def test_worktree_identity_uses_module_owned_sandbox_name_and_path(tmp_path):
    identity = worktree_identity("pycastle/merge-sandbox", tmp_path)

    assert identity.branch == "pycastle/merge-sandbox"
    assert identity.name == "merge-sandbox"
    assert identity.path == tmp_path / "pycastle" / ".worktrees" / "merge-sandbox"


def test_worktree_identity_uses_module_owned_issue_sandbox_name_and_path(tmp_path):
    identity = worktree_identity("pycastle/merge-sandbox-issue-42", tmp_path)

    assert identity.branch == "pycastle/merge-sandbox-issue-42"
    assert identity.name == "merge-sandbox-issue-42"
    assert (
        identity.path == tmp_path / "pycastle" / ".worktrees" / "merge-sandbox-issue-42"
    )


def test_worktree_identity_preserves_current_caller_name_override(tmp_path):
    identity = worktree_identity(
        "pycastle/issue-42-add-worktree-identity",
        tmp_path,
        name="issue-42",
    )

    assert identity.branch == "pycastle/issue-42-add-worktree-identity"
    assert identity.name == "issue-42"
    assert identity.path == tmp_path / "pycastle" / ".worktrees" / "issue-42"


# ── managed_worktree: WorktreeIdentity interface ─────────────────────────────


def test_managed_worktree_accepts_worktree_identity(branch_deps):
    identity = worktree_identity("pycastle/merge-sandbox", branch_deps.repo_root)
    branch_deps.git_svc.is_working_tree_clean.return_value = True

    async def _run():
        async with managed_worktree(
            identity=identity,
            sha="abc123",
            delete_branch_on_teardown=True,
            deps=branch_deps,
        ) as path:
            assert path == identity.path

    asyncio.run(_run())

    branch_deps.git_svc.create_worktree.assert_called_once_with(
        branch_deps.repo_root,
        identity.path,
        identity.branch,
        "abc123",
    )
    branch_deps.git_svc.delete_branch.assert_called_once_with(
        identity.branch,
        branch_deps.repo_root,
    )


# ── transient_worktree ────────────────────────────────────────────────────────


@pytest.fixture
def detached_deps(tmp_path):
    mock_svc = MagicMock(spec=GitService)
    cfg = Config()
    return SimpleNamespace(repo_root=tmp_path, cfg=cfg, git_svc=mock_svc)


def test_transient_worktree_creates_worktree_on_enter(detached_deps):
    expected_path = detached_deps.repo_root / "pycastle" / ".worktrees" / "sandbox"

    async def _run():
        async with transient_worktree("sandbox", sha="abc123", deps=detached_deps):
            detached_deps.git_svc.checkout_detached.assert_called_once_with(
                detached_deps.repo_root, expected_path, "abc123"
            )

    asyncio.run(_run())


def test_transient_worktree_yields_correct_path(detached_deps):
    expected_path = detached_deps.repo_root / "pycastle" / ".worktrees" / "sandbox"

    async def _run():
        async with transient_worktree(
            "sandbox", sha="abc123", deps=detached_deps
        ) as path:
            assert path == expected_path

    asyncio.run(_run())


def test_transient_worktree_removes_worktree_on_clean_exit(detached_deps):
    expected_path = detached_deps.repo_root / "pycastle" / ".worktrees" / "sandbox"

    async def _run():
        async with transient_worktree("sandbox", sha="abc123", deps=detached_deps):
            pass

    asyncio.run(_run())
    detached_deps.git_svc.remove_worktree.assert_called_once_with(
        detached_deps.repo_root, expected_path
    )


def test_transient_worktree_removes_worktree_when_body_raises(detached_deps):
    expected_path = detached_deps.repo_root / "pycastle" / ".worktrees" / "sandbox"

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
    expected_path = detached_deps.repo_root / "pycastle" / ".worktrees" / "sandbox"

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


def test_transient_worktree_marks_preserved_failure_on_agent_failed_error(
    detached_deps,
):
    marker = (
        detached_deps.repo_root
        / "pycastle"
        / ".worktrees"
        / "sandbox"
        / ".pycastle-session"
        / ".preserved-failure"
    )

    async def _run():
        with pytest.raises(AgentFailedError):
            async with transient_worktree("sandbox", sha="abc123", deps=detached_deps):
                raise AgentFailedError("planner", Path("sandbox"))

    asyncio.run(_run())

    detached_deps.git_svc.remove_worktree.assert_not_called()
    assert marker.is_file()


# ── managed_worktree ──────────────────────────────────────────────────────────


def test_managed_worktree_creates_worktree_on_enter_and_yields_correct_path(
    branch_deps,
):
    expected_path = branch_deps.repo_root / "pycastle" / ".worktrees" / "issue-42"

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
    expected_path = branch_deps.repo_root / "pycastle" / ".worktrees" / "issue-42"

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


def test_managed_worktree_preserves_worktree_on_unexpected_exception(branch_deps):
    expected_path = branch_deps.repo_root / "pycastle" / ".worktrees" / "issue-42"

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
    branch_deps.git_svc.remove_worktree.assert_not_called()
    branch_deps.git_svc.delete_branch.assert_not_called()
    marker = expected_path / ".pycastle-session" / ".preserved-failure"
    assert marker.is_file()


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
    worktrees_dir = real_branch_deps.repo_root / "pycastle" / ".worktrees"

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
    worktrees_dir = real_branch_deps.repo_root / "pycastle" / ".worktrees"

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
    worktrees_dir = real_branch_deps.repo_root / "pycastle" / ".worktrees"
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


def test_managed_worktree_cleans_up_on_usage_limit_error(branch_deps):
    """UsageLimitError stays on its handled path and must not preserve the worktree."""
    expected_path = branch_deps.repo_root / "pycastle" / ".worktrees" / "issue-42"

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

    branch_deps.git_svc.remove_worktree.assert_called_once_with(
        branch_deps.repo_root, expected_path
    )
    branch_deps.git_svc.delete_branch.assert_called_once_with(
        "pycastle/issue-42", branch_deps.repo_root
    )
    marker = expected_path / ".pycastle-session" / ".preserved-failure"
    assert not marker.exists()


def test_managed_worktree_preserves_worktree_on_agent_failed_error(branch_deps):
    """managed_worktree must not teardown when AgentFailedError propagates."""

    async def _run():
        with pytest.raises(AgentFailedError):
            async with managed_worktree(
                "issue-42",
                branch="pycastle/issue-42",
                sha=None,
                delete_branch_on_teardown=True,
                deps=branch_deps,
            ) as wt_path:
                raise AgentFailedError(
                    role_value="implementer",
                    worktree_path=wt_path,
                )

    asyncio.run(_run())

    branch_deps.git_svc.remove_worktree.assert_not_called()
    branch_deps.git_svc.delete_branch.assert_not_called()


# ── managed_worktree: parametrised preservation predicate ────────────────────


@pytest.mark.parametrize(
    "dirty, usage_limit_exc, has_resumable_session, expected_teardown",
    [
        (False, False, False, True),
        (True, False, False, False),
        (False, True, False, True),
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
    """preserve iff dirty OR has_resumable_session for handled usage-limit exits."""
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


def test_managed_worktree_cleans_up_on_transient_agent_error(branch_deps):
    """Transient provider failures stay non-preserving at the worktree boundary."""
    expected_path = branch_deps.repo_root / "pycastle" / ".worktrees" / "issue-42"

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

    branch_deps.git_svc.remove_worktree.assert_called_once_with(
        branch_deps.repo_root, expected_path
    )
    branch_deps.git_svc.delete_branch.assert_called_once_with(
        "pycastle/issue-42", branch_deps.repo_root
    )


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


def test_managed_worktree_cleans_up_on_agent_credential_failure(branch_deps):
    """Credential failures are terminal but not preservation-worthy by themselves."""
    expected_path = branch_deps.repo_root / "pycastle" / ".worktrees" / "issue-42"

    async def _run():
        with pytest.raises(AgentCredentialFailureError):
            async with managed_worktree(
                "issue-42",
                branch="pycastle/issue-42",
                sha=None,
                delete_branch_on_teardown=True,
                deps=branch_deps,
            ):
                raise AgentCredentialFailureError(
                    message="Codex authentication missing: run `codex login` on the host.",
                    status_code=401,
                    service_name="codex",
                    observations=(),
                )

    asyncio.run(_run())

    branch_deps.git_svc.remove_worktree.assert_called_once_with(
        branch_deps.repo_root, expected_path
    )
    branch_deps.git_svc.delete_branch.assert_called_once_with(
        "pycastle/issue-42", branch_deps.repo_root
    )
    marker = expected_path / ".pycastle-session" / ".preserved-failure"
    assert not marker.exists()


@pytest.mark.parametrize("dirty, has_resumable_session", [(True, False), (False, True)])
def test_managed_worktree_preserves_independent_worktree_state_on_agent_credential_failure(
    branch_deps, dirty, has_resumable_session
):
    """Dirty or resumable work still preserves after a credential failure."""
    branch_deps.git_svc.is_working_tree_clean.return_value = not dirty

    async def _run():
        with pytest.raises(AgentCredentialFailureError):
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
                raise AgentCredentialFailureError(
                    message="Codex authentication missing: run `codex login` on the host.",
                    status_code=401,
                    service_name="codex",
                    observations=(),
                )

    asyncio.run(_run())

    branch_deps.git_svc.remove_worktree.assert_not_called()
    branch_deps.git_svc.delete_branch.assert_not_called()
    marker = (
        branch_deps.repo_root
        / "pycastle"
        / ".worktrees"
        / "issue-42"
        / ".pycastle-session"
        / ".preserved-failure"
    )
    assert not marker.exists()


# ── prune_orphan_worktrees ────────────────────────────────────────────────────


def _make_prune_git_svc(active_paths: list[Path]) -> GitService:
    mock_svc = MagicMock(spec=GitService)
    mock_svc.list_worktrees.return_value = active_paths
    return mock_svc


def test_prune_orphan_worktrees_uses_fixed_project_local_pycastle_dir(tmp_path):
    """Orphans are swept from pycastle/.worktrees even when config carries a stale pycastle_dir."""
    from pycastle.infrastructure.worktree import prune_orphan_worktrees

    cfg = Config()
    worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)
    orphan = worktrees_dir / "orphan-branch"
    orphan.mkdir()

    prune_orphan_worktrees(tmp_path, cfg=cfg, git_service=_make_prune_git_svc([]))

    assert not orphan.exists()


def test_prune_orphan_worktrees_default_pycastle_dir_still_works(tmp_path):
    """With the default pycastle_dir='pycastle', orphans under pycastle/.worktrees/ are removed."""
    from pycastle.infrastructure.worktree import prune_orphan_worktrees

    cfg = Config()
    worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)
    orphan = worktrees_dir / "orphan-branch"
    orphan.mkdir()

    prune_orphan_worktrees(tmp_path, cfg=cfg, git_service=_make_prune_git_svc([]))

    assert not orphan.exists()


def test_prune_orphan_worktrees_preserves_unregistered_failure_worktree(tmp_path):
    from pycastle.infrastructure.worktree import prune_orphan_worktrees

    cfg = Config()
    worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)
    preserved = worktrees_dir / "issue-99"
    preserved.mkdir()
    marker = preserved / ".pycastle-session" / ".preserved-failure"
    marker.parent.mkdir(parents=True)
    marker.write_text("")

    prune_orphan_worktrees(tmp_path, cfg=cfg, git_service=_make_prune_git_svc([]))

    assert preserved.exists()


def test_prune_orphan_worktrees_removes_worktrees_parent_when_empty(tmp_path):
    """Parent .worktrees dir is removed when empty after orphan sweep."""
    from pycastle.infrastructure.worktree import prune_orphan_worktrees

    cfg = Config()
    worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)
    orphan = worktrees_dir / "orphan-branch"
    orphan.mkdir()

    prune_orphan_worktrees(tmp_path, cfg=cfg, git_service=_make_prune_git_svc([]))

    assert not worktrees_dir.exists()


def test_prune_orphan_worktrees_sweeps_fixed_pycastle_dir_even_when_config_is_stale(
    tmp_path,
):
    """The fixed pycastle/.worktrees location is swept even when config carries a stale pycastle_dir."""
    from pycastle.infrastructure.worktree import prune_orphan_worktrees

    cfg = Config()
    old_worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    old_worktrees_dir.mkdir(parents=True)
    orphan_in_old_location = old_worktrees_dir / "orphan"
    orphan_in_old_location.mkdir()

    prune_orphan_worktrees(tmp_path, cfg=cfg, git_service=_make_prune_git_svc([]))

    assert not orphan_in_old_location.exists()


def test_prune_orphan_worktrees_uses_injected_git_service_without_loading_config(
    tmp_path,
):
    """Injected git_service is the seam; fixed-path pruning must not depend on config load."""
    from pycastle.infrastructure import worktree as worktree_module

    worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)
    orphan = worktrees_dir / "orphan-branch"
    orphan.mkdir()

    with patch.object(
        worktree_module, "load_config", side_effect=AssertionError("unexpected load")
    ):
        worktree_module.prune_orphan_worktrees(
            tmp_path,
            git_service=_make_prune_git_svc([]),
        )

    assert not orphan.exists()


# ── prune_orphan_worktrees: git-registered worktrees without role sessions ────


@pytest.fixture
def registered_orphan(repo):
    """A git-registered worktree on pycastle/issue-99 with no role session."""
    from pycastle.infrastructure.worktree import prune_orphan_worktrees

    cfg = Config()
    worktrees_dir = repo / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)
    wt_path = worktrees_dir / "issue-99"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b",
         "pycastle/issue-99", str(wt_path), "HEAD"],
        check=True, capture_output=True,
    )  # fmt: skip
    return SimpleNamespace(
        repo=repo,
        cfg=cfg,
        svc=GitService(cfg),
        wt_path=wt_path,
        branch="pycastle/issue-99",
        sweep=lambda: prune_orphan_worktrees(
            repo, cfg=cfg, git_service=GitService(cfg)
        ),
    )


def _branch_exists(repo: Path, branch: str) -> bool:
    out = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", branch],
        capture_output=True,
        text=True,
    ).stdout
    return branch in out


def test_prune_orphan_worktrees_preserves_registered_worktree_with_no_role_session(
    registered_orphan,
):
    registered_orphan.sweep()

    assert registered_orphan.wt_path in registered_orphan.svc.list_worktrees(
        registered_orphan.repo
    )
    assert registered_orphan.wt_path.exists()


def test_prune_orphan_worktrees_keeps_empty_branch_for_registered_worktree_without_role_session(
    registered_orphan,
):
    registered_orphan.sweep()

    assert _branch_exists(registered_orphan.repo, registered_orphan.branch)


def test_prune_orphan_worktrees_preserves_branch_with_commits_ahead_of_main(
    registered_orphan,
):
    wt_path = registered_orphan.wt_path
    (wt_path / "work.txt").write_text("real work")
    subprocess.run(
        ["git", "-C", str(wt_path), "add", "work.txt"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(wt_path), "commit", "-m", "real work"],
        check=True,
        capture_output=True,
    )

    registered_orphan.sweep()

    assert _branch_exists(registered_orphan.repo, registered_orphan.branch)


def test_prune_orphan_worktrees_does_not_tear_down_worktree_with_role_session(
    registered_orphan,
):
    (registered_orphan.wt_path / ".pycastle-session" / "implementer").mkdir(
        parents=True
    )

    registered_orphan.sweep()

    assert registered_orphan.wt_path in registered_orphan.svc.list_worktrees(
        registered_orphan.repo
    )
    assert registered_orphan.wt_path.exists()


def test_prune_orphan_worktrees_preserves_failure_worktree_without_role_session(
    registered_orphan,
):
    (registered_orphan.wt_path / ".pycastle-session").mkdir(parents=True)
    (registered_orphan.wt_path / ".pycastle-session" / ".preserved-failure").write_text(
        ""
    )

    registered_orphan.sweep()

    assert registered_orphan.wt_path in registered_orphan.svc.list_worktrees(
        registered_orphan.repo
    )
    assert registered_orphan.wt_path.exists()


# ── managed_worktree: empty-branch cleanup overrides delete_branch_on_teardown ─


def test_managed_worktree_preserves_branch_with_commits_when_delete_branch_on_teardown_false(
    real_branch_deps,
):
    """With delete_branch_on_teardown=False, a branch with at least one commit ahead of
    main must not be deleted on the teardown path — it represents WIP."""

    async def _run():
        async with managed_worktree(
            "issue-wip",
            branch="pycastle/issue-wip",
            sha=None,
            delete_branch_on_teardown=False,
            deps=real_branch_deps,
        ) as path:
            (path / "wip.txt").write_text("work in progress")
            subprocess.run(
                ["git", "-C", str(path), "add", "wip.txt"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(path), "commit", "-m", "wip commit"],
                check=True,
                capture_output=True,
            )

    asyncio.run(_run())

    branches = subprocess.run(
        [
            "git",
            "-C",
            str(real_branch_deps.repo_root),
            "branch",
            "--list",
            "pycastle/issue-wip",
        ],
        capture_output=True,
        text=True,
    ).stdout
    assert "pycastle/issue-wip" in branches


def test_durable_issue_worktree_preserves_issue_branch_with_commits_ahead_of_main(
    real_branch_deps,
):
    async def _run():
        async with durable_issue_worktree(42, sha=None, deps=real_branch_deps) as path:
            (path / "wip.txt").write_text("work in progress")
            subprocess.run(
                ["git", "-C", str(path), "add", "wip.txt"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(path), "commit", "-m", "wip commit"],
                check=True,
                capture_output=True,
            )

    asyncio.run(_run())

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


def test_managed_worktree_deletes_empty_branch_when_delete_branch_on_teardown_false(
    real_branch_deps,
):
    """With delete_branch_on_teardown=False, an empty branch (zero commits ahead of main)
    must be deleted on the teardown path — empty branches are not WIP."""
    worktree_path: Path | None = None

    async def _run():
        nonlocal worktree_path
        async with managed_worktree(
            "issue-empty",
            branch="pycastle/issue-empty",
            sha=None,
            delete_branch_on_teardown=False,
            deps=real_branch_deps,
        ) as path:
            worktree_path = path

    asyncio.run(_run())

    assert worktree_path is not None
    assert not worktree_path.exists()
    branches = subprocess.run(
        [
            "git",
            "-C",
            str(real_branch_deps.repo_root),
            "branch",
            "--list",
            "pycastle/issue-empty",
        ],
        capture_output=True,
        text=True,
    ).stdout
    assert "pycastle/issue-empty" not in branches


# ── managed_worktree: ephemeral sandbox rebuild guarantee (issue #896) ────────


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_ephemeral_sandbox_rebuilds_at_sha_when_stale_divergent_branch_exists(repo):
    """AC#1: delete_branch_on_teardown=True + divergent stale branch → worktree HEAD is sha."""
    cfg = Config()
    deps = SimpleNamespace(repo_root=repo, cfg=cfg, git_svc=GitService(cfg))

    # sha_base: HEAD of the repo fixture (has pyproject.toml)
    sha_base = _git(repo, "rev-parse", "HEAD")

    # Advance main with a new commit → sha_main
    (repo / "main_extra.txt").write_text("extra")
    _git(repo, "add", "main_extra.txt")
    _git(repo, "commit", "-m", "main extra")
    sha_main = _git(repo, "rev-parse", "HEAD")

    # Create sandbox branch at sha_base and add a diverging commit
    _git(repo, "branch", "pycastle/merge-sandbox", sha_base)
    temp_wt = repo.parent / "temp-sandbox-wt"
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            str(temp_wt),
            "pycastle/merge-sandbox",
        ],
        check=True,
        capture_output=True,
    )
    (temp_wt / "sandbox_work.txt").write_text("stale sandbox work")
    _git(temp_wt, "add", "sandbox_work.txt")
    _git(temp_wt, "commit", "-m", "stale sandbox commit")
    sha_sandbox = _git(temp_wt, "rev-parse", "HEAD")
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", str(temp_wt)],
        check=True,
        capture_output=True,
    )

    # Precondition: the sandbox branch now diverges from main
    assert sha_sandbox != sha_main
    assert sha_sandbox != sha_base

    # Enter ephemeral sandbox with sha=sha_main — must produce HEAD at sha_main
    head_inside: list[str] = []

    async def _run():
        async with managed_worktree(
            "merge-sandbox",
            branch="pycastle/merge-sandbox",
            sha=sha_main,
            delete_branch_on_teardown=True,
            deps=deps,
        ) as path:
            head_inside.append(_git(path, "rev-parse", "HEAD"))

    asyncio.run(_run())

    assert head_inside == [sha_main], (
        f"worktree HEAD was {head_inside[0]!r}, expected sha_main={sha_main!r}"
    )


def test_ephemeral_sandbox_reuses_preserved_failure_worktree_by_default(repo):
    """AC#2: preserved failure sandboxes stay intact on the default fresh-sandbox path."""
    cfg = Config()
    deps = SimpleNamespace(repo_root=repo, cfg=cfg, git_svc=GitService(cfg))

    # sha_base: HEAD of the repo fixture
    sha_base = _git(repo, "rev-parse", "HEAD")

    # Advance main with a new commit → sha_main
    (repo / "main_extra2.txt").write_text("extra2")
    _git(repo, "add", "main_extra2.txt")
    _git(repo, "commit", "-m", "main extra2")
    sha_main = _git(repo, "rev-parse", "HEAD")

    # Simulate a stale sandbox worktree at sha_base with a role dir (is_worktree_reusable=True)
    wt_dir = repo / "pycastle" / ".worktrees" / "merge-sandbox"
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "-b",
            "pycastle/merge-sandbox",
            str(wt_dir),
            sha_base,
        ],
        check=True,
        capture_output=True,
    )
    # Add a diverging commit to the stale sandbox worktree
    (wt_dir / "stale.txt").write_text("stale")
    _git(wt_dir, "add", "stale.txt")
    _git(wt_dir, "commit", "-m", "stale sandbox commit")
    sha_stale = _git(wt_dir, "rev-parse", "HEAD")

    # Plant a role dir so is_worktree_reusable returns True
    (wt_dir / ".pycastle-session" / "merger").mkdir(parents=True)
    (wt_dir / ".pycastle-session" / ".preserved-failure").write_text("")

    assert sha_stale != sha_main

    # Enter ephemeral sandbox with sha=sha_main — preserved failure evidence must survive.
    head_inside: list[str] = []

    async def _run():
        async with managed_worktree(
            "merge-sandbox",
            branch="pycastle/merge-sandbox",
            sha=sha_main,
            delete_branch_on_teardown=True,
            deps=deps,
        ) as path:
            head_inside.append(_git(path, "rev-parse", "HEAD"))

    asyncio.run(_run())

    assert head_inside == [sha_stale], (
        f"worktree HEAD was {head_inside[0]!r}, expected preserved sha_stale={sha_stale!r}"
    )
    assert (wt_dir / ".pycastle-session" / ".preserved-failure").is_file()


def test_ephemeral_sandbox_can_replace_preserved_failure_worktree_when_requested(repo):
    """AC#3: callers can explicitly request a fresh sandbox replacement."""
    cfg = Config()
    deps = SimpleNamespace(repo_root=repo, cfg=cfg, git_svc=GitService(cfg))

    sha_base = _git(repo, "rev-parse", "HEAD")

    (repo / "main_extra_replace.txt").write_text("extra replace")
    _git(repo, "add", "main_extra_replace.txt")
    _git(repo, "commit", "-m", "main extra replace")
    sha_main = _git(repo, "rev-parse", "HEAD")

    wt_dir = repo / "pycastle" / ".worktrees" / "merge-sandbox"
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "-b",
            "pycastle/merge-sandbox",
            str(wt_dir),
            sha_base,
        ],
        check=True,
        capture_output=True,
    )
    (wt_dir / "stale-replace.txt").write_text("stale replace")
    _git(wt_dir, "add", "stale-replace.txt")
    _git(wt_dir, "commit", "-m", "stale replace commit")
    (wt_dir / ".pycastle-session" / "merger").mkdir(parents=True)
    (wt_dir / ".pycastle-session" / ".preserved-failure").write_text("")

    head_inside: list[str] = []

    async def _run():
        async with managed_worktree(
            "merge-sandbox",
            branch="pycastle/merge-sandbox",
            sha=sha_main,
            delete_branch_on_teardown=True,
            replace_preserved_failure=True,
            deps=deps,
        ) as path:
            head_inside.append(_git(path, "rev-parse", "HEAD"))

    asyncio.run(_run())

    assert head_inside == [sha_main], (
        f"worktree HEAD was {head_inside[0]!r}, expected fresh sha_main={sha_main!r}"
    )
    assert not (wt_dir / ".pycastle-session" / ".preserved-failure").exists()


def test_non_ephemeral_worktree_reuses_existing_branch_tip(repo):
    """AC#3: delete_branch_on_teardown=False + existing branch → worktree HEAD is branch tip (sha ignored)."""
    cfg = Config()
    deps = SimpleNamespace(repo_root=repo, cfg=cfg, git_svc=GitService(cfg))

    sha_base = _git(repo, "rev-parse", "HEAD")

    # Advance main → sha_main
    (repo / "main_extra3.txt").write_text("extra3")
    _git(repo, "add", "main_extra3.txt")
    _git(repo, "commit", "-m", "main extra3")
    sha_main = _git(repo, "rev-parse", "HEAD")

    # Create issue branch at sha_base with a commit (sha_branch diverges from main)
    _git(repo, "branch", "pycastle/issue-123", sha_base)
    temp_wt2 = repo.parent / "temp-issue-wt"
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            str(temp_wt2),
            "pycastle/issue-123",
        ],
        check=True,
        capture_output=True,
    )
    (temp_wt2 / "impl.txt").write_text("impl work")
    _git(temp_wt2, "add", "impl.txt")
    _git(temp_wt2, "commit", "-m", "impl")
    sha_branch = _git(temp_wt2, "rev-parse", "HEAD")
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", str(temp_wt2)],
        check=True,
        capture_output=True,
    )

    assert sha_branch != sha_main

    # Enter non-ephemeral worktree with sha=sha_main — sha is ignored, must land at sha_branch
    head_inside: list[str] = []

    async def _run():
        async with managed_worktree(
            "issue-123",
            branch="pycastle/issue-123",
            sha=sha_main,
            delete_branch_on_teardown=False,
            deps=deps,
        ) as path:
            head_inside.append(_git(path, "rev-parse", "HEAD"))

    asyncio.run(_run())

    assert head_inside == [sha_branch], (
        f"worktree HEAD was {head_inside[0]!r}, expected sha_branch={sha_branch!r}"
    )


def test_reusable_sandbox_rebuilds_stale_non_preserved_branch_at_sha(repo):
    """Reusable sandboxes replace stale non-preserved state so HEAD matches the requested SHA."""
    cfg = Config()
    deps = SimpleNamespace(repo_root=repo, cfg=cfg, git_svc=GitService(cfg))

    sha_base = _git(repo, "rev-parse", "HEAD")

    (repo / "main_extra_reusable.txt").write_text("extra reusable")
    _git(repo, "add", "main_extra_reusable.txt")
    _git(repo, "commit", "-m", "main extra reusable")
    sha_main = _git(repo, "rev-parse", "HEAD")

    _git(repo, "branch", "pycastle/improve-sandbox", sha_base)
    temp_wt = repo.parent / "temp-improve-sandbox-wt"
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            str(temp_wt),
            "pycastle/improve-sandbox",
        ],
        check=True,
        capture_output=True,
    )
    (temp_wt / "stale_sandbox_work.txt").write_text("stale reusable sandbox work")
    _git(temp_wt, "add", "stale_sandbox_work.txt")
    _git(temp_wt, "commit", "-m", "stale reusable sandbox commit")
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", str(temp_wt)],
        check=True,
        capture_output=True,
    )

    head_inside: list[str] = []

    async def _run():
        async with reusable_sandbox_worktree(
            "improve-sandbox",
            sha=sha_main,
            deps=deps,
        ) as path:
            head_inside.append(_git(path, "rev-parse", "HEAD"))

    asyncio.run(_run())

    assert head_inside == [sha_main], (
        f"worktree HEAD was {head_inside[0]!r}, expected sha_main={sha_main!r}"
    )


def test_reusable_sandbox_keeps_preserved_failure_state_intact(repo):
    """Reusable sandboxes leave preserved failure state in place instead of silently replacing it."""
    cfg = Config()
    deps = SimpleNamespace(repo_root=repo, cfg=cfg, git_svc=GitService(cfg))

    sha_base = _git(repo, "rev-parse", "HEAD")

    (repo / "main_extra_preserved.txt").write_text("extra preserved")
    _git(repo, "add", "main_extra_preserved.txt")
    _git(repo, "commit", "-m", "main extra preserved")
    sha_main = _git(repo, "rev-parse", "HEAD")

    wt_dir = repo / "pycastle" / ".worktrees" / "improve-sandbox"
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "-b",
            "pycastle/improve-sandbox",
            str(wt_dir),
            sha_base,
        ],
        check=True,
        capture_output=True,
    )
    (wt_dir / "stale-preserved.txt").write_text("stale preserved")
    _git(wt_dir, "add", "stale-preserved.txt")
    _git(wt_dir, "commit", "-m", "stale preserved commit")
    sha_stale = _git(wt_dir, "rev-parse", "HEAD")
    (wt_dir / ".pycastle-session" / "improve").mkdir(parents=True)
    (wt_dir / ".pycastle-session" / ".preserved-failure").write_text("")

    head_inside: list[str] = []

    async def _run():
        async with reusable_sandbox_worktree(
            "improve-sandbox",
            sha=sha_main,
            deps=deps,
        ) as path:
            head_inside.append(_git(path, "rev-parse", "HEAD"))

    asyncio.run(_run())

    assert head_inside == [sha_stale], (
        f"worktree HEAD was {head_inside[0]!r}, expected preserved sha_stale={sha_stale!r}"
    )
    assert (wt_dir / ".pycastle-session" / ".preserved-failure").is_file()


def test_reusable_sandbox_tears_down_clean_branch_on_success(repo):
    """Reusable sandboxes remove their clean worktree and branch after success."""
    cfg = Config()
    deps = SimpleNamespace(repo_root=repo, cfg=cfg, git_svc=GitService(cfg))

    sha_main = _git(repo, "rev-parse", "HEAD")
    sandbox_path = repo / "pycastle" / ".worktrees" / "improve-sandbox"

    async def _run():
        async with reusable_sandbox_worktree(
            "improve-sandbox",
            sha=sha_main,
            deps=deps,
        ) as path:
            assert path == sandbox_path
            assert _git(path, "rev-parse", "HEAD") == sha_main

    asyncio.run(_run())

    assert not sandbox_path.exists()
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "pycastle/improve-sandbox"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "pycastle/improve-sandbox" not in branches
