import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pycastle.container_runner import ContainerRunner, run_agent


# ── helpers ──────────────────────────────────────────────────────────────────

def _fake_runner(exit_code=0, stdout=b"", stderr=b""):
    """ContainerRunner with mocked Docker container, bypassing __init__."""
    runner = object.__new__(ContainerRunner)
    runner.name = "test"
    runner.env = {}
    runner._container_env = {}
    runner.branch = None
    runner._worktree_path = "/home/agent/workspace"
    runner._container = MagicMock()
    mock_result = MagicMock()
    mock_result.exit_code = exit_code
    mock_result.output = (stdout, stderr)
    runner._container.exec_run.return_value = mock_result
    return runner


def _run(coro):
    return asyncio.run(coro)


# ── Cycle 1: exec_simple raises on non-zero exit ──────────────────────────────

def test_exec_simple_raises_on_nonzero_exit():
    runner = _fake_runner(exit_code=1, stderr=b"command failed")
    with pytest.raises(RuntimeError, match="command failed"):
        runner.exec_simple("exit 1")


def test_exec_simple_returns_stdout_on_success():
    runner = _fake_runner(exit_code=0, stdout=b"hello\n")
    assert runner.exec_simple("echo hello") == "hello\n"


# ── Cycle 2: pip install failure does not crash run_agent ────────────────────

class _PipFailRunner:
    """Fake ContainerRunner: exec_simple raises when pip is the command."""

    def __init__(self, *args, **kwargs):
        self.branch = None
        self.env = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def exec_simple(self, cmd, timeout=None):
        if "pip" in cmd:
            raise RuntimeError("pip install failed: no matching distribution")
        return ""

    def write_file(self, *args):
        pass

    def run_streaming(self):
        return ""


def test_run_agent_proceeds_when_pip_install_fails(tmp_path):
    prompt = tmp_path / "p.md"
    prompt.write_text("Plain prompt.")

    with patch("pycastle.container_runner.ContainerRunner", _PipFailRunner):
        result = _run(run_agent("Test", prompt, tmp_path, {}))

    assert result == ""


def test_run_agent_warns_stderr_when_pip_install_fails(tmp_path, capsys):
    prompt = tmp_path / "p.md"
    prompt.write_text("Plain prompt.")

    with patch("pycastle.container_runner.ContainerRunner", _PipFailRunner):
        _run(run_agent("Test", prompt, tmp_path, {}))

    assert "warning" in capsys.readouterr().err.lower()


# ── Cycle 3: two agents run concurrently ─────────────────────────────────────

_DELAY = 0.08  # per-stage delay for each fake runner (s)


class _SlowFakeRunner:
    """Fake ContainerRunner that sleeps during __enter__ and pip install."""

    def __init__(self, *args, **kwargs):
        self.branch = None
        self.env = {}

    def __enter__(self):
        time.sleep(_DELAY)
        return self

    def __exit__(self, *args):
        pass

    def exec_simple(self, cmd, timeout=None):
        if "pip" in cmd:
            time.sleep(_DELAY)
        return ""

    def write_file(self, *args):
        pass

    def run_streaming(self):
        return ""


def test_two_agents_run_concurrently(tmp_path):
    """Two concurrent run_agent calls must interleave rather than serialize.

    Each agent sleeps _DELAY in __enter__ and another _DELAY in pip install.
    Sequential execution would take ≥ 4 * _DELAY; concurrent takes ≈ 2 * _DELAY.
    """
    prompt = tmp_path / "p.md"
    prompt.write_text("Plain prompt.")

    async def _both():
        return await asyncio.gather(
            run_agent("A1", prompt, tmp_path, {}),
            run_agent("A2", prompt, tmp_path, {}),
        )

    with patch("pycastle.container_runner.ContainerRunner", _SlowFakeRunner):
        start = time.monotonic()
        _run(_both())
        elapsed = time.monotonic() - start

    # Must finish well under sequential time (4 * _DELAY = 0.32 s).
    # Generous ceiling of 3 * _DELAY leaves room for CI overhead.
    assert elapsed < 3 * _DELAY, (
        f"Agents appear to be running sequentially: {elapsed:.3f}s >= {3 * _DELAY:.3f}s"
    )


# ── Cycle 15: worktree add must not run inside the container ─────────────────

@patch("pycastle.container_runner.LOGS_DIR")
@patch("pycastle.container_runner.docker")
def test_worktree_add_not_called_inside_container(mock_docker, mock_logs_dir, tmp_path):
    """When worktree_host_path is provided, ContainerRunner must not exec worktree add."""
    mock_container = MagicMock()
    mock_docker.from_env.return_value.containers.run.return_value = mock_container
    mock_container.exec_run.return_value = MagicMock(exit_code=0, output=b"")

    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    runner = ContainerRunner(
        "test", tmp_path, {}, branch="feature/test", worktree_host_path=worktree_path
    )
    runner.__enter__()
    runner.__exit__(None, None, None)

    all_exec_cmds = [
        " ".join(call.args[0]) if isinstance(call.args[0], list) else str(call.args[0])
        for call in mock_container.exec_run.call_args_list
    ]
    assert not any("worktree add" in cmd for cmd in all_exec_cmds), (
        f"worktree add was called inside the container: {all_exec_cmds}"
    )


# ── Cycle 16: implementer mounts worktree dir at /home/agent/workspace ────────

@patch("pycastle.container_runner.LOGS_DIR")
@patch("pycastle.container_runner.docker")
def test_implementer_mounts_worktree_at_workspace(mock_docker, mock_logs_dir, tmp_path):
    """When worktree_host_path is provided the container must bind it at /home/agent/workspace."""
    mock_container = MagicMock()
    mock_docker.from_env.return_value.containers.run.return_value = mock_container
    mock_container.exec_run.return_value = MagicMock(exit_code=0, output=b"")

    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    runner = ContainerRunner(
        "test", tmp_path, {}, branch="feature/test", worktree_host_path=worktree_path
    )
    runner.__enter__()
    runner.__exit__(None, None, None)

    volumes = mock_docker.from_env.return_value.containers.run.call_args.kwargs["volumes"]
    bound_paths = {v["bind"]: k for k, v in volumes.items()}
    assert "/home/agent/workspace" in bound_paths, (
        f"/home/agent/workspace not mounted; volumes={volumes}"
    )
    assert bound_paths["/home/agent/workspace"] == str(worktree_path.resolve()).replace("\\", "/"), (
        f"Wrong host path mounted at /home/agent/workspace: {bound_paths['/home/agent/workspace']!r}"
    )


# ── Cycle 17: host-side worktree removed even when container raises ───────────

class _StreamingErrorRunner:
    """Fake runner that succeeds setup but raises during run_streaming."""

    def __init__(self, *args, **kwargs):
        self.branch = None
        self.env = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def exec_simple(self, cmd, timeout=None):
        return ""

    def write_file(self, *_):
        pass

    def run_streaming(self):
        raise RuntimeError("container crashed mid-run")


def test_host_worktree_removed_even_when_container_raises(tmp_path):
    """remove_worktree must be called on the host in finally, even if the container throws."""
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    with patch("pycastle.container_runner.ContainerRunner", _StreamingErrorRunner), \
         patch("pycastle.container_runner.create_worktree") as mock_create, \
         patch("pycastle.container_runner.remove_worktree") as mock_remove:
        with pytest.raises(RuntimeError, match="container crashed"):
            _run(run_agent("test", prompt, tmp_path, {}, branch="feature/test"))

    mock_remove.assert_called_once()


# ── Cycle 8: no container is started when host-side worktree creation fails ───

@patch("pycastle.container_runner.LOGS_DIR")
@patch("pycastle.container_runner.docker")
def test_no_container_started_when_worktree_creation_fails(mock_docker, mock_logs_dir, tmp_path):
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    with patch("pycastle.container_runner.create_worktree",
               side_effect=RuntimeError("git worktree add failed")), \
         pytest.raises(RuntimeError, match="worktree add failed"):
        _run(run_agent("test", prompt, tmp_path, {}, branch="feature/test"))

    mock_docker.from_env.return_value.containers.run.assert_not_called()


# ── Cycle 4: exec_simple raises TimeoutError on stalled command ───────────────

def test_exec_simple_times_out():
    blocker = threading.Event()
    runner = _fake_runner()
    # Make exec_run block until the event is set
    runner._container.exec_run.side_effect = lambda *a, **kw: blocker.wait() or None

    try:
        with pytest.raises(TimeoutError):
            runner.exec_simple("sleep inf", timeout=0.05)
    finally:
        blocker.set()  # release the background thread
