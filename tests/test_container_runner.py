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


# ── Cycle 5: __enter__ raises RuntimeError on worktree failure ───────────────

@patch("pycastle.container_runner.LOGS_DIR")
@patch("pycastle.container_runner.docker")
def test_enter_raises_on_worktree_failure(mock_docker, mock_logs_dir, tmp_path):
    mock_container = MagicMock()
    mock_docker.from_env.return_value.containers.run.return_value = mock_container

    fail_result = MagicMock()
    fail_result.exit_code = 1
    fail_result.output = b"fatal: branch 'no-such-branch' not found"
    mock_container.exec_run.return_value = fail_result

    runner = ContainerRunner("test", tmp_path, {}, branch="no-such-branch")
    with pytest.raises(RuntimeError):
        runner.__enter__()


# ── Cycle 7: __enter__ raises RuntimeError when project files missing from worktree ──

@patch("pycastle.container_runner.LOGS_DIR")
@patch("pycastle.container_runner.docker")
def test_enter_raises_when_project_files_missing(mock_docker, mock_logs_dir, tmp_path):
    mock_container = MagicMock()
    mock_docker.from_env.return_value.containers.run.return_value = mock_container

    prune_ok = MagicMock()
    prune_ok.exit_code = 0

    rev_parse_ok = MagicMock()
    rev_parse_ok.exit_code = 0

    worktree_ok = MagicMock()
    worktree_ok.exit_code = 0
    worktree_ok.output = b""

    files_absent = MagicMock()
    files_absent.exit_code = 1  # test -e returns 1 when file is absent

    ls_ok = MagicMock()
    ls_ok.exit_code = 0
    ls_ok.output = b""
    mock_container.exec_run.side_effect = [prune_ok, rev_parse_ok, worktree_ok, files_absent, ls_ok]

    runner = ContainerRunner("test", tmp_path, {}, branch="feature/fix")
    with pytest.raises(RuntimeError, match="(?i)commit"):
        runner.__enter__()


# ── Cycle 8: container is cleaned up when worktree setup fails ───────────────

@patch("pycastle.container_runner.LOGS_DIR")
@patch("pycastle.container_runner.docker")
def test_container_cleaned_up_when_worktree_setup_fails(mock_docker, mock_logs_dir, tmp_path):
    mock_container = MagicMock()
    mock_docker.from_env.return_value.containers.run.return_value = mock_container

    fail_result = MagicMock()
    fail_result.exit_code = 1
    fail_result.output = b"fatal: already registered worktree"
    mock_container.exec_run.return_value = fail_result

    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    with pytest.raises(Exception):
        _run(run_agent("test", prompt, tmp_path, {}, branch="feature/test"))

    mock_container.stop.assert_called()
    mock_container.remove.assert_called()


# ── Cycle 9: parallel implementers use distinct worktree paths ───────────────

@patch("pycastle.container_runner.LOGS_DIR")
@patch("pycastle.container_runner.docker")
def test_parallel_implementers_use_distinct_worktree_paths(mock_docker, mock_logs_dir, tmp_path):
    """Two runners with different branches must issue worktree add with different paths."""
    import re

    worktree_paths_seen = []

    def recording_exec_run(cmd, **kwargs):
        if isinstance(cmd, list):
            cmd_str = " ".join(cmd)
            # Extract the worktree path: first /home/agent/... token after "worktree add"
            m = re.search(r"worktree add (?:-b \S+ )?(/home/agent/\S+)", cmd_str)
            if m:
                worktree_paths_seen.append(m.group(1))
        result = MagicMock()
        result.exit_code = 0
        result.output = b""
        return result

    containers = [MagicMock(), MagicMock()]
    for c in containers:
        c.exec_run.side_effect = recording_exec_run
    mock_docker.from_env.return_value.containers.run.side_effect = containers

    runner1 = ContainerRunner("r1", tmp_path, {}, branch="sandcastle/issue-2-foo")
    runner2 = ContainerRunner("r2", tmp_path, {}, branch="sandcastle/issue-3-bar")
    runner1.__enter__()
    runner2.__enter__()
    runner1.__exit__(None, None, None)
    runner2.__exit__(None, None, None)

    assert len(worktree_paths_seen) >= 2
    assert worktree_paths_seen[0] != worktree_paths_seen[1], (
        f"Both runners used the same worktree path '{worktree_paths_seen[0]}' — collision"
    )


# ── Cycle 10: stale worktree registration is pruned before worktree add ──────

@patch("pycastle.container_runner.LOGS_DIR")
@patch("pycastle.container_runner.docker")
def test_worktree_prune_runs_before_worktree_add(mock_docker, mock_logs_dir, tmp_path):
    """_setup_worktree must prune stale registrations before attempting worktree add."""
    call_order = []

    def recording_exec_run(cmd, **kwargs):
        if isinstance(cmd, list):
            cmd_str = " ".join(cmd)
            if "worktree prune" in cmd_str:
                call_order.append("prune")
            elif "worktree add" in cmd_str:
                call_order.append("add")
        result = MagicMock()
        result.exit_code = 0
        result.output = b""
        return result

    mock_container = MagicMock()
    mock_container.exec_run.side_effect = recording_exec_run
    mock_docker.from_env.return_value.containers.run.return_value = mock_container

    runner = ContainerRunner("test", tmp_path, {}, branch="feature/test")
    runner.__enter__()
    runner.__exit__(None, None, None)

    assert "prune" in call_order, "git worktree prune was never called"
    assert "add" in call_order, "git worktree add was never called"
    assert call_order.index("prune") < call_order.index("add"), (
        "git worktree prune must run before git worktree add"
    )


# ── Cycle 11: existing branch is checked out without -b ──────────────────────

@patch("pycastle.container_runner.LOGS_DIR")
@patch("pycastle.container_runner.docker")
def test_existing_branch_checked_out_without_create_flag(mock_docker, mock_logs_dir, tmp_path):
    """When the branch already exists, worktree add must not use -b (create-new)."""
    worktree_add_cmds = []

    def recording_exec_run(cmd, **kwargs):
        if isinstance(cmd, list):
            cmd_str = " ".join(cmd)
            if "worktree add" in cmd_str and "worktree remove" not in cmd_str:
                worktree_add_cmds.append(cmd_str)
        result = MagicMock()
        result.exit_code = 0
        result.output = b""
        return result

    mock_container = MagicMock()
    mock_container.exec_run.side_effect = recording_exec_run
    mock_docker.from_env.return_value.containers.run.return_value = mock_container

    # Simulate an existing branch by making rev-parse succeed
    runner = ContainerRunner("test", tmp_path, {}, branch="feature/existing")
    runner.__enter__()
    runner.__exit__(None, None, None)

    assert worktree_add_cmds, "No worktree add command was issued"
    final_add = worktree_add_cmds[-1]
    assert " -b " not in final_add, (
        f"worktree add used -b for an existing branch: {final_add!r}"
    )


# ── Cycle 14: missing-files error includes worktree path and ls output ────────

@patch("pycastle.container_runner.LOGS_DIR")
@patch("pycastle.container_runner.docker")
def test_missing_project_files_error_includes_worktree_path_and_listing(mock_docker, mock_logs_dir, tmp_path):
    mock_container = MagicMock()
    mock_docker.from_env.return_value.containers.run.return_value = mock_container

    prune_ok = MagicMock(exit_code=0)
    rev_parse_ok = MagicMock(exit_code=0)
    worktree_ok = MagicMock(exit_code=0, output=b"")
    files_absent = MagicMock(exit_code=1)
    ls_result = MagicMock(exit_code=0, output=b"only_random_files.txt\n")

    mock_container.exec_run.side_effect = [prune_ok, rev_parse_ok, worktree_ok, files_absent, ls_result]

    runner = ContainerRunner("test", tmp_path, {}, branch="feature/fix")
    with pytest.raises(RuntimeError) as exc_info:
        runner.__enter__()

    msg = str(exc_info.value)
    assert "/home/agent/workspace-feature-fix" in msg, f"worktree path missing from error: {msg!r}"
    assert "only_random_files.txt" in msg, f"ls output missing from error: {msg!r}"


# ── Cycle 12: new-branch worktree add includes HEAD as final argument ─────────

@patch("pycastle.container_runner.LOGS_DIR")
@patch("pycastle.container_runner.docker")
def test_new_branch_worktree_add_ends_with_HEAD(mock_docker, mock_logs_dir, tmp_path):
    """For a new branch, git worktree add must end with HEAD."""
    worktree_add_cmds = []

    def recording_exec_run(cmd, **kwargs):
        if isinstance(cmd, list):
            cmd_str = " ".join(cmd)
            if "worktree add" in cmd_str and "worktree remove" not in cmd_str:
                worktree_add_cmds.append(cmd_str)
            if "rev-parse" in cmd_str:
                result = MagicMock()
                result.exit_code = 1
                result.output = b""
                return result
        result = MagicMock()
        result.exit_code = 0
        result.output = b""
        return result

    mock_container = MagicMock()
    mock_container.exec_run.side_effect = recording_exec_run
    mock_docker.from_env.return_value.containers.run.return_value = mock_container

    runner = ContainerRunner("test", tmp_path, {}, branch="feature/new")
    runner.__enter__()
    runner.__exit__(None, None, None)

    assert worktree_add_cmds, "No worktree add command was issued"
    assert worktree_add_cmds[-1].split()[-1] == "HEAD", (
        f"worktree add for new branch does not end with HEAD: {worktree_add_cmds[-1]!r}"
    )


# ── Cycle 13: existing-branch worktree add uses branch name as final token ────

@patch("pycastle.container_runner.LOGS_DIR")
@patch("pycastle.container_runner.docker")
def test_existing_branch_worktree_add_ends_with_branch_name(mock_docker, mock_logs_dir, tmp_path):
    """For an existing branch, git worktree add must end with the branch name."""
    worktree_add_cmds = []

    def recording_exec_run(cmd, **kwargs):
        if isinstance(cmd, list):
            cmd_str = " ".join(cmd)
            if "worktree add" in cmd_str and "worktree remove" not in cmd_str:
                worktree_add_cmds.append(cmd_str)
        result = MagicMock()
        result.exit_code = 0
        result.output = b""
        return result

    mock_container = MagicMock()
    mock_container.exec_run.side_effect = recording_exec_run
    mock_docker.from_env.return_value.containers.run.return_value = mock_container

    branch = "feature/existing"
    runner = ContainerRunner("test", tmp_path, {}, branch=branch)
    runner.__enter__()
    runner.__exit__(None, None, None)

    assert worktree_add_cmds, "No worktree add command was issued"
    assert worktree_add_cmds[-1].split()[-1] == branch, (
        f"worktree add for existing branch does not end with branch name: {worktree_add_cmds[-1]!r}"
    )


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
