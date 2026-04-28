import asyncio
from unittest.mock import MagicMock

import pytest

from pycastle.container_runner import ContainerRunner, _setup
from pycastle.git_service import GitService
from pycastle.worktree import (
    create_worktree,
    patch_gitdir_for_container,
    remove_worktree,
)


def _docker_available() -> bool:
    try:
        import docker as _docker

        _docker.from_env().ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _docker_available(), reason="Docker not available")


# ── Cycle 1: _setup succeeds with read-only repo mount ───────────────────────


def test_setup_configures_git_identity_with_readonly_repo_mount(git_repo):
    """_setup must configure git identity without error in the worktree/branch case.

    When a branch is used, /home/agent/repo is mounted read-only.  A local
    `git config` write targets that read-only mount and fails with exit 255.
    The fix is to use `git config --global` so the write goes to ~/.gitconfig,
    which is always writable inside the container.
    """
    import subprocess

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

    worktree_path = git_repo / ".pycastle" / ".worktrees" / "test-branch"
    create_worktree(git_repo, worktree_path, "test-branch")
    gitdir_overlay = patch_gitdir_for_container(worktree_path)

    runner = ContainerRunner(
        "integration-test",
        git_repo,
        {},
        branch="test-branch",
        worktree_host_path=worktree_path,
        gitdir_overlay=gitdir_overlay,
    )
    mock_git = MagicMock(spec=GitService)
    mock_git.get_user_name.return_value = "Test User"
    mock_git.get_user_email.return_value = "test@example.com"

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            _setup("integration-test", runner, loop, 30.0, git_service=mock_git)
        )

        assert (
            runner.exec_simple("git config --global user.name", 10.0).strip()
            == "Test User"
        )
        assert (
            runner.exec_simple("git config --global user.email", 10.0).strip()
            == "test@example.com"
        )
    finally:
        runner.__exit__(None, None, None)
        loop.close()
        if gitdir_overlay:
            gitdir_overlay.unlink(missing_ok=True)
        remove_worktree(git_repo, worktree_path)
