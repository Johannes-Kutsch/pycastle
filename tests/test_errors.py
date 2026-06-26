import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pycastle.errors import (
    AgentFailedError,
    AgentTimeoutError,
    BranchCollisionError,
    DockerError,
    DockerTimeoutError,
    HardAgentError,
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


def test_usage_limit_error_carries_reset_time():
    from datetime import datetime

    from pycastle.errors import UsageLimitError

    reset = datetime(2026, 5, 4, 12, 50)
    err = UsageLimitError(reset_time=reset)
    assert err.reset_time == reset


def test_usage_limit_error_reset_time_defaults_to_none():
    from pycastle.errors import UsageLimitError

    err = UsageLimitError()
    assert err.reset_time is None


def test_pycastle_error_shims_preserve_legacy_service_and_session_defaults():
    hard_error = HardAgentError(
        message="provider rejected request", status_code=400, service_name="claude"
    )
    failed_error = AgentFailedError(
        role_value="implementer",
        worktree_path=Path("."),
        service_name="claude",
    )

    assert hard_error.service_name == "claude"
    assert failed_error.service_name == "claude"
    assert str(failed_error.session_store) == ".pycastle-session/implementer/claude"


def test_runtime_derived_pycastle_compatibility_errors_subclass_pycastle_error():
    assert issubclass(HardAgentError, PycastleError)
    assert issubclass(AgentFailedError, PycastleError)


# ── Raise sites ───────────────────────────────────────────────────────────────


@pytest.fixture
def fake_session(tmp_path):
    from pycastle.config import Config
    from pycastle.infrastructure.docker_session import DockerSession

    def _make(exit_code=0, stdout=b"", stderr=b""):
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.exit_code = exit_code
        mock_result.output = (stdout, stderr)
        mock_client.containers.run.return_value.exec_run.return_value = mock_result

        session = DockerSession(
            volumes={},
            container_env={},
            image_name="img",
            cfg=Config(logs_dir=tmp_path / "logs"),
            docker_client=mock_client,
        )
        session.__enter__()
        return session

    return _make


def test_exec_simple_raises_docker_error_on_nonzero_exit(fake_session):
    session = fake_session(exit_code=1, stderr=b"command failed")
    with pytest.raises(DockerError):
        session.exec_simple("exit 1")


def test_exec_simple_raises_docker_timeout_error_on_timeout(fake_session):
    blocker = threading.Event()
    session = fake_session()
    session._container.exec_run.side_effect = lambda *a, **kw: blocker.wait() or None
    try:
        with pytest.raises(DockerTimeoutError):
            session.exec_simple("sleep inf", timeout=0.05)
    finally:
        blocker.set()


def test_branch_worktree_raises_worktree_error_on_git_failure(tmp_path):
    """Git can't check out the same branch in two worktrees — must raise WorktreeError."""
    import asyncio
    from types import SimpleNamespace

    from pycastle.config import Config
    from pycastle.services import GitService
    from pycastle.infrastructure.worktree import (
        BranchWorktreeLifecycle,
        managed_worktree,
    )

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

    cfg = Config()
    deps = SimpleNamespace(repo_root=tmp_path, cfg=cfg, git_svc=GitService(cfg))

    async def _run():
        async with managed_worktree(
            "name1",
            branch="feature/same",
            sha=None,
            lifecycle=BranchWorktreeLifecycle.DURABLE_ISSUE,
            deps=deps,
        ):
            with pytest.raises(WorktreeError):
                async with managed_worktree(
                    "name2",
                    branch="feature/same",
                    sha=None,
                    lifecycle=BranchWorktreeLifecycle.REUSABLE_SANDBOX,
                    deps=deps,
                ):
                    pass

    asyncio.run(_run())
