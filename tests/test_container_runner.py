import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pycastle.container_runner import ContainerRunner, run_agent
from pycastle.errors import AgentTimeoutError


# ── helpers ──────────────────────────────────────────────────────────────────

def _streaming_runner(name: str, chunks: list, log_path) -> ContainerRunner:
    """ContainerRunner whose run_streaming replays the given byte chunks."""
    runner = object.__new__(ContainerRunner)
    runner.name = name
    runner.env = {}
    runner._container_env = {}
    runner.branch = None
    runner._worktree_path = "/home/agent/workspace"
    runner._container = MagicMock()
    runner._log_path = log_path
    mock_result = MagicMock()
    mock_result.output = iter(chunks)
    runner._container.exec_run.return_value = mock_result
    return runner


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
    # Generous ceiling of 3.5 * _DELAY leaves room for CI overhead.
    assert elapsed < 3.5 * _DELAY, (
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


# ── Cycle 32-2: gitdir overlay bound at /home/agent/workspace/.git ───────────

@patch("pycastle.container_runner.LOGS_DIR")
@patch("pycastle.container_runner.docker")
def test_container_mounts_gitdir_overlay_at_workspace_git(mock_docker, mock_logs_dir, tmp_path):
    """When gitdir_overlay is set, ContainerRunner must bind-mount it at /home/agent/workspace/.git."""
    mock_container = MagicMock()
    mock_docker.from_env.return_value.containers.run.return_value = mock_container

    overlay_file = tmp_path / "gitdir_overlay"
    overlay_file.write_text("gitdir: /home/agent/repo/.git/worktrees/my-branch\n")
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    runner = ContainerRunner(
        "test", tmp_path, {},
        branch="feature/test",
        worktree_host_path=worktree_path,
        gitdir_overlay=overlay_file,
    )
    runner.__enter__()
    runner.__exit__(None, None, None)

    volumes = mock_docker.from_env.return_value.containers.run.call_args.kwargs["volumes"]
    bound_paths = {v["bind"]: k for k, v in volumes.items()}
    assert "/home/agent/workspace/.git" in bound_paths, (
        f"/home/agent/workspace/.git not mounted; volumes={volumes}"
    )
    assert bound_paths["/home/agent/workspace/.git"] == str(overlay_file.resolve()).replace("\\", "/"), (
        f"Wrong host path at /home/agent/workspace/.git: {bound_paths['/home/agent/workspace/.git']!r}"
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


# ── Cycle 22: phase logging ───────────────────────────────────────────────────

class _PhaseLogRunner:
    """Minimal fake runner for phase logging and git-identity tests."""

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
        return ""


def test_run_agent_logs_setup_phase(tmp_path, capsys):
    prompt = tmp_path / "p.md"
    prompt.write_text("Plain prompt.")

    with patch("pycastle.container_runner.ContainerRunner", _PhaseLogRunner):
        _run(run_agent("Test", prompt, tmp_path, {}))

    assert "[Test] Phase: Setup" in capsys.readouterr().out


def test_run_agent_logs_prepare_phase(tmp_path, capsys):
    prompt = tmp_path / "p.md"
    prompt.write_text("Plain prompt.")

    with patch("pycastle.container_runner.ContainerRunner", _PhaseLogRunner):
        _run(run_agent("Test", prompt, tmp_path, {}))

    assert "[Test] Phase: Prepare" in capsys.readouterr().out


def test_run_agent_logs_work_phase(tmp_path, capsys):
    prompt = tmp_path / "p.md"
    prompt.write_text("Plain prompt.")

    with patch("pycastle.container_runner.ContainerRunner", _PhaseLogRunner):
        _run(run_agent("Test", prompt, tmp_path, {}))

    assert "[Test] Phase: Work" in capsys.readouterr().out


# ── Cycle 22: git identity injection ─────────────────────────────────────────

def _make_exec_logging_runner():
    """Return (RunnerClass, exec_log) — exec_log collects exec_simple calls."""
    exec_log: list[str] = []

    class _Runner:
        def __init__(self, *args, **kwargs):
            self.branch = None
            self.env = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def exec_simple(self, cmd, timeout=None):
            exec_log.append(cmd)
            return ""

        def write_file(self, *_):
            pass

        def run_streaming(self):
            return ""

    return _Runner, exec_log


def _git_mock(name="Alice", email="alice@example.com"):
    def _check_output(cmd, **kw):
        if "user.name" in cmd:
            return f"{name}\n"
        return f"{email}\n"
    return _check_output


def test_setup_injects_host_git_name(tmp_path):
    prompt = tmp_path / "p.md"
    prompt.write_text("test")
    _Runner, exec_log = _make_exec_logging_runner()

    with patch("pycastle.container_runner.ContainerRunner", _Runner), \
         patch("pycastle.container_runner.subprocess.check_output", side_effect=_git_mock()):
        _run(run_agent("Test", prompt, tmp_path, {}))

    assert any("git config --global user.name" in cmd and "Alice" in cmd for cmd in exec_log)


def test_setup_injects_host_git_email(tmp_path):
    prompt = tmp_path / "p.md"
    prompt.write_text("test")
    _Runner, exec_log = _make_exec_logging_runner()

    with patch("pycastle.container_runner.ContainerRunner", _Runner), \
         patch("pycastle.container_runner.subprocess.check_output", side_effect=_git_mock()):
        _run(run_agent("Test", prompt, tmp_path, {}))

    assert any("git config --global user.email" in cmd and "alice@example.com" in cmd for cmd in exec_log)


# ── Cycle 23-4: run_streaming raises AgentTimeoutError on idle timeout ────────

def _never_yields():
    """Generator that blocks forever without yielding — simulates a hung agent."""
    event = threading.Event()
    event.wait()
    return
    yield  # makes this a generator


def test_run_streaming_raises_agent_timeout_error_when_idle(tmp_path):
    runner = _fake_runner()
    mock_result = MagicMock()
    mock_result.output = _never_yields()
    runner._container.exec_run.return_value = mock_result
    runner._log_path = tmp_path / "test.log"

    with patch("pycastle.container_runner.IDLE_TIMEOUT", 0.05):
        with pytest.raises(AgentTimeoutError):
            runner.run_streaming()


# ── Cycle 23-5: branch collision lock ────────────────────────────────────────

def test_second_run_agent_on_same_branch_raises_branch_collision_error(tmp_path):
    from pycastle.errors import BranchCollisionError

    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    async def _two_on_same_branch():
        with patch("pycastle.container_runner.ContainerRunner", _PhaseLogRunner), \
             patch("pycastle.container_runner.create_worktree"), \
             patch("pycastle.container_runner.remove_worktree"):
            return await asyncio.gather(
                run_agent("A1", prompt, tmp_path, {}, branch="feature/collision"),
                run_agent("A2", prompt, tmp_path, {}, branch="feature/collision"),
                return_exceptions=True,
            )

    results = asyncio.run(_two_on_same_branch())
    errors = [r for r in results if isinstance(r, Exception)]
    assert any(isinstance(e, BranchCollisionError) for e in errors), (
        f"Expected BranchCollisionError, got: {errors}"
    )


def test_run_agent_different_branches_both_succeed(tmp_path):
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    async def _two_different_branches():
        with patch("pycastle.container_runner.ContainerRunner", _PhaseLogRunner), \
             patch("pycastle.container_runner.create_worktree"), \
             patch("pycastle.container_runner.remove_worktree"):
            return await asyncio.gather(
                run_agent("B1", prompt, tmp_path, {}, branch="feature/branch-one"),
                run_agent("B2", prompt, tmp_path, {}, branch="feature/branch-two"),
                return_exceptions=True,
            )

    results = asyncio.run(_two_different_branches())
    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, f"Expected both agents to succeed, got errors: {errors}"


# ── Cycle 24-A1: run_streaming prefixes lines in stdout ──────────────────────

def test_run_streaming_prefixes_complete_lines_in_stdout(tmp_path, capsys):
    runner = _streaming_runner("TestAgent", [b"hello world\n"], tmp_path / "test.log")
    runner.run_streaming()
    assert "[TestAgent] hello world" in capsys.readouterr().out


def test_run_streaming_prefixes_each_line_separately(tmp_path, capsys):
    """Multiple lines in a single chunk must each get their own prefix."""
    runner = _streaming_runner("Bot", [b"line one\nline two\n"], tmp_path / "test.log")
    runner.run_streaming()
    out = capsys.readouterr().out
    assert "[Bot] line one" in out
    assert "[Bot] line two" in out


def test_run_streaming_handles_chunks_split_across_newlines(tmp_path, capsys):
    """A line split across two chunks must be assembled before prefixing."""
    runner = _streaming_runner("Bot", [b"hel", b"lo\n"], tmp_path / "test.log")
    runner.run_streaming()
    assert "[Bot] hello" in capsys.readouterr().out


# ── Cycle 24-A2: log file stays raw (unprefixed) ─────────────────────────────

def test_run_streaming_log_file_is_raw_unprefixed(tmp_path):
    log_path = tmp_path / "test.log"
    runner = _streaming_runner("TestAgent", [b"hello world\n"], log_path)
    runner.run_streaming()
    assert log_path.read_text() == "hello world\n"
    assert "[TestAgent]" not in log_path.read_text()


def test_run_streaming_log_file_contains_full_raw_output(tmp_path):
    """Log file must capture all raw bytes, including multi-chunk output."""
    log_path = tmp_path / "test.log"
    runner = _streaming_runner("Bot", [b"line one\n", b"line two\n"], log_path)
    runner.run_streaming()
    content = log_path.read_text()
    assert content == "line one\nline two\n"


# ── Cycle 25-A: container cleanup raises → worktree cleanup still runs ────────

class _ContainerExitErrorRunner:
    def __init__(self, *_, **__): pass
    def __enter__(self): return self
    def __exit__(self, *_): raise RuntimeError("container stop failed")
    def exec_simple(self, cmd, timeout=None): return ""
    def write_file(self, *_): pass
    def run_streaming(self): return "done"


def test_worktree_cleanup_runs_even_when_container_cleanup_raises(tmp_path):
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    with patch("pycastle.container_runner.ContainerRunner", _ContainerExitErrorRunner), \
         patch("pycastle.container_runner.create_worktree"), \
         patch("pycastle.container_runner.remove_worktree") as mock_remove:
        with pytest.raises(RuntimeError, match="container stop failed"):
            _run(run_agent("test", prompt, tmp_path, {}, branch="feature/test"))

    mock_remove.assert_called_once()


# ── Cycle 25-B: worktree cleanup raises → container cleanup still runs ────────

def test_container_cleanup_runs_even_when_worktree_cleanup_raises(tmp_path):
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    container_exit_calls = []

    class _TrackingRunner:
        def __init__(self, *_, **__): pass
        def __enter__(self): return self
        def __exit__(self, *_): container_exit_calls.append(True)
        def exec_simple(self, cmd, timeout=None): return ""
        def write_file(self, *_): pass
        def run_streaming(self): return "done"

    with patch("pycastle.container_runner.ContainerRunner", _TrackingRunner), \
         patch("pycastle.container_runner.create_worktree"), \
         patch("pycastle.container_runner.remove_worktree", side_effect=RuntimeError("worktree removal failed")):
        with pytest.raises(RuntimeError, match="worktree removal failed"):
            _run(run_agent("test", prompt, tmp_path, {}, branch="feature/test"))

    assert len(container_exit_calls) == 1


# ── Cycle 32-3: gitdir temp file cleaned up after run_agent ──────────────────

class _SuccessRunner:
    def __init__(self, *_, **__): pass
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def exec_simple(self, cmd, timeout=None): return ""
    def write_file(self, *_): pass
    def run_streaming(self): return "done"


def test_gitdir_temp_file_deleted_after_run_agent_succeeds(tmp_path):
    """The temp file returned by patch_gitdir_for_container must be deleted after run_agent."""
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    overlay = tmp_path / "gitdir_temp"
    overlay.write_text("gitdir: /home/agent/repo/.git/worktrees/test\n")

    with patch("pycastle.container_runner.ContainerRunner", _SuccessRunner), \
         patch("pycastle.container_runner.create_worktree"), \
         patch("pycastle.container_runner.remove_worktree"), \
         patch("pycastle.container_runner.patch_gitdir_for_container", return_value=overlay):
        _run(run_agent("test", prompt, tmp_path, {}, branch="feature/test"))

    assert not overlay.exists(), "gitdir temp file must be deleted after run_agent"


def test_gitdir_temp_file_deleted_even_when_container_raises(tmp_path):
    """The temp file must be deleted even when the container cleanup raises."""
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    overlay = tmp_path / "gitdir_temp_err"
    overlay.write_text("gitdir: /home/agent/repo/.git/worktrees/test\n")

    with patch("pycastle.container_runner.ContainerRunner", _ContainerExitErrorRunner), \
         patch("pycastle.container_runner.create_worktree"), \
         patch("pycastle.container_runner.remove_worktree"), \
         patch("pycastle.container_runner.patch_gitdir_for_container", return_value=overlay):
        with pytest.raises(RuntimeError):
            _run(run_agent("test", prompt, tmp_path, {}, branch="feature/test"))

    assert not overlay.exists(), "gitdir temp file must be deleted even when container cleanup raises"
