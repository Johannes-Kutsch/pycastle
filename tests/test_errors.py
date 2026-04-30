import subprocess
import threading
from unittest.mock import MagicMock

import pytest

from pycastle.errors import (
    AgentTimeoutError,
    BranchCollisionError,
    DockerError,
    DockerTimeoutError,
    PycastleError,
    WorktreeError,
    WorktreeTimeoutError,
)


# ── Hierarchy ─────────────────────────────────────────────────────────────────


def test_worktree_error_is_runtime_error():
    assert issubclass(WorktreeError, RuntimeError)


def test_worktree_error_is_pycastle_error():
    assert issubclass(WorktreeError, PycastleError)


def test_docker_error_is_runtime_error():
    assert issubclass(DockerError, RuntimeError)


def test_docker_error_is_pycastle_error():
    assert issubclass(DockerError, PycastleError)


def test_worktree_timeout_error_is_worktree_error_and_timeout_error():
    assert issubclass(WorktreeTimeoutError, WorktreeError)
    assert issubclass(WorktreeTimeoutError, TimeoutError)


def test_docker_timeout_error_is_docker_error_and_timeout_error():
    assert issubclass(DockerTimeoutError, DockerError)
    assert issubclass(DockerTimeoutError, TimeoutError)


def test_agent_timeout_error_is_pycastle_error_and_timeout_error():
    assert issubclass(AgentTimeoutError, PycastleError)
    assert issubclass(AgentTimeoutError, TimeoutError)


def test_branch_collision_error_is_worktree_error():
    assert issubclass(BranchCollisionError, WorktreeError)


# ── UsageLimitError ───────────────────────────────────────────────────────────


def test_usage_limit_error_is_pycastle_error():
    from pycastle.errors import UsageLimitError

    assert issubclass(UsageLimitError, PycastleError)


def test_usage_limit_error_carries_matched_line():
    from pycastle.errors import UsageLimitError

    err = UsageLimitError("You've hit your session limit")
    assert str(err) == "You've hit your session limit"


# ── PreflightError ────────────────────────────────────────────────────────────


def test_preflight_error_is_pycastle_error():
    from pycastle.errors import PreflightError

    assert issubclass(PreflightError, PycastleError)


def test_preflight_error_carries_failure_tuples():
    from pycastle.errors import PreflightError

    failures = [("ruff", "ruff check .", "E501 line too long")]
    err = PreflightError(failures)
    assert err.failures == failures


# ── Raise sites ───────────────────────────────────────────────────────────────


def _fake_runner(tmp_path, exit_code=0, stdout=b"", stderr=b""):
    from pycastle.config import Config
    from pycastle.container_runner import ContainerRunner

    mock_client = MagicMock()
    cfg = Config(logs_dir=tmp_path / "logs")
    runner = ContainerRunner(
        name="test",
        mount_path=tmp_path,
        env={},
        docker_client=mock_client,
        cfg=cfg,
    )
    runner.__enter__()
    mock_result = MagicMock()
    mock_result.exit_code = exit_code
    mock_result.output = (stdout, stderr)
    runner._container.exec_run.return_value = mock_result
    return runner


def test_exec_simple_raises_docker_error_on_nonzero_exit(tmp_path):
    runner = _fake_runner(tmp_path, exit_code=1, stderr=b"command failed")
    with pytest.raises(DockerError):
        runner.exec_simple("exit 1")


def test_exec_simple_raises_docker_timeout_error_on_timeout(tmp_path):
    blocker = threading.Event()
    runner = _fake_runner(tmp_path)
    runner._container.exec_run.side_effect = lambda *a, **kw: blocker.wait() or None
    try:
        with pytest.raises(DockerTimeoutError):
            runner.exec_simple("sleep inf", timeout=0.05)
    finally:
        blocker.set()


def test_create_worktree_raises_worktree_error_on_git_failure(tmp_path):
    """Git can't check out the same branch in two worktrees — must raise WorktreeError."""
    from pycastle.worktree import create_worktree

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "T"],
        check=True,
        capture_output=True,
    )
    (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )

    wt = tmp_path / "wt"
    create_worktree(tmp_path, wt, "feature/same")
    with pytest.raises(WorktreeError):
        create_worktree(tmp_path, tmp_path / "wt2", "feature/same")
