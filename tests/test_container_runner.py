import asyncio
import shutil
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pycastle.container_runner import (
    ContainerRunner,
    _build_claude_command,
    _format_stream_line,
    run_agent,
)
from pycastle.errors import AgentTimeoutError
from pycastle.git_service import (
    GitCommandError,
    GitNotFoundError,
    GitService,
    GitTimeoutError,
)


# ── Issue 153: docker_client injection ───────────────────────────────────────


def test_container_runner_init_uses_injected_docker_client():
    """ContainerRunner must accept docker_client and use it instead of docker.from_env()."""
    mock_client = MagicMock()
    runner = ContainerRunner("test", Path("/fake"), {}, docker_client=mock_client)
    assert runner._client is mock_client


def test_container_runner_init_calls_docker_from_env_when_no_client_given():
    """When docker_client is None, __init__ must call docker.from_env()."""
    with patch("pycastle.container_runner.docker") as mock_docker:
        runner = ContainerRunner("test", Path("/fake"), {})
    assert runner._client is mock_docker.from_env.return_value


# ── helpers ──────────────────────────────────────────────────────────────────


def _streaming_runner(name: str, chunks: list, log_path) -> ContainerRunner:
    """ContainerRunner whose run_streaming replays the given byte chunks."""
    runner = ContainerRunner(name, Path("/fake"), {}, docker_client=MagicMock())
    runner._log_path = log_path
    mock_result = MagicMock()
    mock_result.output = iter(chunks)
    runner._container = MagicMock()
    runner._container.exec_run.return_value = mock_result
    return runner


def _fake_runner(exit_code=0, stdout=b"", stderr=b""):
    """ContainerRunner with mocked Docker container."""
    runner = ContainerRunner("test", Path("/fake"), {}, docker_client=MagicMock())
    mock_result = MagicMock()
    mock_result.exit_code = exit_code
    mock_result.output = (stdout, stderr)
    runner._container = MagicMock()
    runner._container.exec_run.return_value = mock_result
    return runner


def _run(coro):
    return asyncio.run(coro)


def _noop_create(repo, wt, branch):
    pass


def _noop_remove(repo, wt):
    pass


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


def test_two_agents_run_concurrently(tmp_path):
    """Two concurrent run_agent calls must interleave rather than serialize.

    A threading.Barrier(2) placed in __enter__ blocks each agent's thread until
    both are inside simultaneously. If agents run sequentially the barrier times
    out, which is structurally impossible with concurrent execution.
    """
    barrier = threading.Barrier(2, timeout=5.0)

    class _BarrierRunner:
        def __init__(self, *args, **kwargs):
            self.branch = None
            self.env = {}

        def __enter__(self):
            barrier.wait()
            return self

        def __exit__(self, *args):
            pass

        def exec_simple(self, cmd, timeout=None):
            return ""

        def run_streaming(self):
            return ""

    prompt = tmp_path / "p.md"
    prompt.write_text("Plain prompt.")

    async def _both():
        return await asyncio.gather(
            run_agent("A1", prompt, tmp_path, {}),
            run_agent("A2", prompt, tmp_path, {}),
        )

    try:
        with patch("pycastle.container_runner.ContainerRunner", _BarrierRunner):
            _run(_both())
    except threading.BrokenBarrierError:
        pytest.fail(
            "Agents ran sequentially: Agent 2 never reached __enter__ while Agent 1 was still inside it"
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

    volumes = mock_docker.from_env.return_value.containers.run.call_args.kwargs[
        "volumes"
    ]
    bound_paths = {v["bind"]: k for k, v in volumes.items()}
    assert "/home/agent/workspace" in bound_paths, (
        f"/home/agent/workspace not mounted; volumes={volumes}"
    )
    assert bound_paths["/home/agent/workspace"] == str(worktree_path.resolve()).replace(
        "\\", "/"
    ), (
        f"Wrong host path mounted at /home/agent/workspace: {bound_paths['/home/agent/workspace']!r}"
    )


# ── Cycle 32-2: gitdir overlay bound at /home/agent/workspace/.git ───────────


@patch("pycastle.container_runner.LOGS_DIR")
@patch("pycastle.container_runner.docker")
def test_container_mounts_gitdir_overlay_at_workspace_git(
    mock_docker, mock_logs_dir, tmp_path
):
    """When gitdir_overlay is set, ContainerRunner must bind-mount it at /home/agent/workspace/.git."""
    mock_container = MagicMock()
    mock_docker.from_env.return_value.containers.run.return_value = mock_container

    overlay_file = tmp_path / "gitdir_overlay"
    overlay_file.write_text("gitdir: /home/agent/repo/.git/worktrees/my-branch\n")
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    runner = ContainerRunner(
        "test",
        tmp_path,
        {},
        branch="feature/test",
        worktree_host_path=worktree_path,
        gitdir_overlay=overlay_file,
    )
    runner.__enter__()
    runner.__exit__(None, None, None)

    volumes = mock_docker.from_env.return_value.containers.run.call_args.kwargs[
        "volumes"
    ]
    bound_paths = {v["bind"]: k for k, v in volumes.items()}
    assert "/home/agent/workspace/.git" in bound_paths, (
        f"/home/agent/workspace/.git not mounted; volumes={volumes}"
    )
    assert bound_paths["/home/agent/workspace/.git"] == str(
        overlay_file.resolve()
    ).replace("\\", "/"), (
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

    def run_streaming(self):
        raise RuntimeError("container crashed mid-run")


def test_host_worktree_removed_even_when_container_raises(tmp_path):
    """remove_worktree_fn must be called on the host in finally, even if the container throws."""
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    worktree_dir = tmp_path / "pycastle" / ".worktrees" / "feature-test"

    def fake_create(repo, wt, branch):
        wt.mkdir(parents=True, exist_ok=True)

    def fake_remove(repo, wt):
        shutil.rmtree(wt, ignore_errors=True)

    with (
        patch("pycastle.container_runner.ContainerRunner", _StreamingErrorRunner),
        pytest.raises(RuntimeError, match="container crashed"),
    ):
        _run(
            run_agent(
                "test",
                prompt,
                tmp_path,
                {},
                branch="feature/test",
                create_worktree_fn=fake_create,
                remove_worktree_fn=fake_remove,
            )
        )

    assert not worktree_dir.exists()


# ── Cycle 8: no container is started when host-side worktree creation fails ───


@patch("pycastle.container_runner.LOGS_DIR")
@patch("pycastle.container_runner.docker")
def test_no_container_started_when_worktree_creation_fails(
    mock_docker, mock_logs_dir, tmp_path
):
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    def failing_create(repo, wt, branch):
        raise RuntimeError("git worktree add failed")

    with pytest.raises(RuntimeError, match="worktree add failed"):
        _run(
            run_agent(
                "test",
                prompt,
                tmp_path,
                {},
                branch="feature/test",
                create_worktree_fn=failing_create,
            )
        )

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

        def run_streaming(self):
            return ""

    return _Runner, exec_log


def _make_git_service(name="Alice", email="alice@example.com") -> MagicMock:
    mock = MagicMock(spec=GitService)
    mock.get_user_name.return_value = name
    mock.get_user_email.return_value = email
    return mock


def test_setup_injects_host_git_name(tmp_path):
    prompt = tmp_path / "p.md"
    prompt.write_text("test")
    _Runner, exec_log = _make_exec_logging_runner()

    with patch("pycastle.container_runner.ContainerRunner", _Runner):
        _run(run_agent("Test", prompt, tmp_path, {}, git_service=_make_git_service()))

    assert any(
        "git config --global user.name" in cmd and "Alice" in cmd for cmd in exec_log
    )


def test_setup_injects_host_git_email(tmp_path):
    prompt = tmp_path / "p.md"
    prompt.write_text("test")
    _Runner, exec_log = _make_exec_logging_runner()

    with patch("pycastle.container_runner.ContainerRunner", _Runner):
        _run(run_agent("Test", prompt, tmp_path, {}, git_service=_make_git_service()))

    assert any(
        "git config --global user.email" in cmd and "alice@example.com" in cmd
        for cmd in exec_log
    )


# ── Issue 90: GitService injection error paths ────────────────────────────────


def test_setup_propagates_git_command_error_from_get_user_name(tmp_path):
    """When GitService.get_user_name() raises GitCommandError, _setup must propagate it."""
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    mock_git = MagicMock(spec=GitService)
    mock_git.get_user_name.side_effect = GitCommandError("git config user.name failed")

    with (
        patch("pycastle.container_runner.ContainerRunner", _PhaseLogRunner),
        pytest.raises(GitCommandError),
    ):
        _run(run_agent("Test", prompt, tmp_path, {}, git_service=mock_git))


def test_setup_propagates_git_command_error_from_get_user_email(tmp_path):
    """When GitService.get_user_email() raises GitCommandError, _setup must propagate it."""
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    mock_git = MagicMock(spec=GitService)
    mock_git.get_user_name.return_value = "Alice"
    mock_git.get_user_email.side_effect = GitCommandError(
        "git config user.email failed"
    )

    with (
        patch("pycastle.container_runner.ContainerRunner", _PhaseLogRunner),
        pytest.raises(GitCommandError),
    ):
        _run(run_agent("Test", prompt, tmp_path, {}, git_service=mock_git))


def test_setup_propagates_git_not_found_error(tmp_path):
    """When git is not installed, GitNotFoundError must propagate from _setup."""
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    mock_git = MagicMock(spec=GitService)
    mock_git.get_user_name.side_effect = GitNotFoundError("git executable not found")

    with (
        patch("pycastle.container_runner.ContainerRunner", _PhaseLogRunner),
        pytest.raises(GitNotFoundError),
    ):
        _run(run_agent("Test", prompt, tmp_path, {}, git_service=mock_git))


def test_setup_propagates_git_timeout_error(tmp_path):
    """When the git command times out, GitTimeoutError must propagate from _setup."""
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    mock_git = MagicMock(spec=GitService)
    mock_git.get_user_name.side_effect = GitTimeoutError("git command timed out")

    with (
        patch("pycastle.container_runner.ContainerRunner", _PhaseLogRunner),
        pytest.raises(GitTimeoutError),
    ):
        _run(run_agent("Test", prompt, tmp_path, {}, git_service=mock_git))


# ── Issue 90: shell-safe quoting of git identity values ──────────────────────


def test_setup_shell_quotes_git_name_containing_single_quote(tmp_path):
    """A git user.name with a single quote (e.g. O'Brien) must produce a valid shell command."""
    import shlex

    prompt = tmp_path / "p.md"
    prompt.write_text("test")
    _Runner, exec_log = _make_exec_logging_runner()

    with patch("pycastle.container_runner.ContainerRunner", _Runner):
        _run(
            run_agent(
                "Test",
                prompt,
                tmp_path,
                {},
                git_service=_make_git_service(name="O'Brien"),
            )
        )

    name_cmds = [cmd for cmd in exec_log if "user.name" in cmd]
    assert name_cmds
    parsed = shlex.split(name_cmds[0])
    assert parsed[-1] == "O'Brien"


def test_setup_shell_quotes_git_email_containing_single_quote(tmp_path):
    """A git user.email with a single quote must produce a valid shell command."""
    import shlex

    prompt = tmp_path / "p.md"
    prompt.write_text("test")
    _Runner, exec_log = _make_exec_logging_runner()

    with patch("pycastle.container_runner.ContainerRunner", _Runner):
        _run(
            run_agent(
                "Test",
                prompt,
                tmp_path,
                {},
                git_service=_make_git_service(email="it's@example.com"),
            )
        )

    email_cmds = [cmd for cmd in exec_log if "user.email" in cmd]
    assert email_cmds
    parsed = shlex.split(email_cmds[0])
    assert parsed[-1] == "it's@example.com"


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
        with patch("pycastle.container_runner.ContainerRunner", _PhaseLogRunner):
            return await asyncio.gather(
                run_agent(
                    "A1",
                    prompt,
                    tmp_path,
                    {},
                    branch="feature/collision",
                    create_worktree_fn=_noop_create,
                    remove_worktree_fn=_noop_remove,
                ),
                run_agent(
                    "A2",
                    prompt,
                    tmp_path,
                    {},
                    branch="feature/collision",
                    create_worktree_fn=_noop_create,
                    remove_worktree_fn=_noop_remove,
                ),
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
        with patch("pycastle.container_runner.ContainerRunner", _PhaseLogRunner):
            return await asyncio.gather(
                run_agent(
                    "B1",
                    prompt,
                    tmp_path,
                    {},
                    branch="feature/branch-one",
                    create_worktree_fn=_noop_create,
                    remove_worktree_fn=_noop_remove,
                ),
                run_agent(
                    "B2",
                    prompt,
                    tmp_path,
                    {},
                    branch="feature/branch-two",
                    create_worktree_fn=_noop_create,
                    remove_worktree_fn=_noop_remove,
                ),
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
    def __init__(self, *_, **__):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        raise RuntimeError("container stop failed")

    def exec_simple(self, cmd, timeout=None):
        return ""

    def run_streaming(self):
        return "done"


def test_worktree_cleanup_runs_even_when_container_cleanup_raises(tmp_path):
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    worktree_dir = tmp_path / "pycastle" / ".worktrees" / "feature-test"

    def fake_create(repo, wt, branch):
        wt.mkdir(parents=True, exist_ok=True)

    def fake_remove(repo, wt):
        shutil.rmtree(wt, ignore_errors=True)

    with (
        patch("pycastle.container_runner.ContainerRunner", _ContainerExitErrorRunner),
        pytest.raises(RuntimeError, match="container stop failed"),
    ):
        _run(
            run_agent(
                "test",
                prompt,
                tmp_path,
                {},
                branch="feature/test",
                create_worktree_fn=fake_create,
                remove_worktree_fn=fake_remove,
            )
        )

    assert not worktree_dir.exists()


# ── Cycle 25-B: worktree cleanup raises → container cleanup still runs ────────


def test_container_cleanup_runs_even_when_worktree_cleanup_raises(tmp_path):
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    container_exit_calls = []

    class _TrackingRunner:
        def __init__(self, *_, **__):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            container_exit_calls.append(True)

        def exec_simple(self, cmd, timeout=None):
            return ""

        def run_streaming(self):
            return "done"

    def failing_remove(repo, wt):
        raise RuntimeError("worktree removal failed")

    with (
        patch("pycastle.container_runner.ContainerRunner", _TrackingRunner),
        pytest.raises(RuntimeError, match="worktree removal failed"),
    ):
        _run(
            run_agent(
                "test",
                prompt,
                tmp_path,
                {},
                branch="feature/test",
                create_worktree_fn=_noop_create,
                remove_worktree_fn=failing_remove,
            )
        )

    assert len(container_exit_calls) == 1


# ── Cycle 32-3: gitdir temp file cleaned up after run_agent ──────────────────


class _SuccessRunner:
    def __init__(self, *_, **__):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def exec_simple(self, cmd, timeout=None):
        return ""

    def run_streaming(self):
        return "done"


def test_gitdir_temp_file_deleted_after_run_agent_succeeds(tmp_path):
    """The temp file returned by patch_gitdir_for_container must be deleted after run_agent."""
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    overlay = tmp_path / "gitdir_temp"
    overlay.write_text("gitdir: /home/agent/repo/.git/worktrees/test\n")

    with (
        patch("pycastle.container_runner.ContainerRunner", _SuccessRunner),
        patch(
            "pycastle.container_runner.patch_gitdir_for_container", return_value=overlay
        ),
    ):
        _run(
            run_agent(
                "test",
                prompt,
                tmp_path,
                {},
                branch="feature/test",
                create_worktree_fn=_noop_create,
                remove_worktree_fn=_noop_remove,
            )
        )

    assert not overlay.exists(), "gitdir temp file must be deleted after run_agent"


def test_gitdir_temp_file_deleted_even_when_container_raises(tmp_path):
    """The temp file must be deleted even when the container cleanup raises."""
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    overlay = tmp_path / "gitdir_temp_err"
    overlay.write_text("gitdir: /home/agent/repo/.git/worktrees/test\n")

    with (
        patch("pycastle.container_runner.ContainerRunner", _ContainerExitErrorRunner),
        patch(
            "pycastle.container_runner.patch_gitdir_for_container", return_value=overlay
        ),
        pytest.raises(RuntimeError),
    ):
        _run(
            run_agent(
                "test",
                prompt,
                tmp_path,
                {},
                branch="feature/test",
                create_worktree_fn=_noop_create,
                remove_worktree_fn=_noop_remove,
            )
        )

    assert not overlay.exists(), (
        "gitdir temp file must be deleted even when container cleanup raises"
    )


# ── Issue 75: _build_claude_command accepts model and effort flags ────────────


def test_build_claude_command_includes_model_flag():
    cmd = _build_claude_command(model="claude-sonnet-4-6")
    assert "--model claude-sonnet-4-6" in cmd


def test_build_claude_command_includes_effort_flag():
    cmd = _build_claude_command(effort="high")
    assert "--effort high" in cmd


def test_build_claude_command_excludes_model_when_empty():
    cmd = _build_claude_command(model="", effort="")
    assert "--model" not in cmd


def test_build_claude_command_excludes_effort_when_empty():
    cmd = _build_claude_command(model="", effort="")
    assert "--effort" not in cmd


def test_build_claude_command_includes_both_flags_when_set():
    cmd = _build_claude_command(model="claude-opus-4-7", effort="high")
    assert "--model claude-opus-4-7" in cmd
    assert "--effort high" in cmd


def test_run_agent_passes_model_to_container_runner(tmp_path):
    """run_agent with model kwarg must pass model to ContainerRunner."""
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    captured: dict = {}

    class _CapturingRunner:
        def __init__(self, *args, model="", effort="", **kwargs):
            self.branch = None
            self.env = {}
            captured["model"] = model
            captured["effort"] = effort

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def exec_simple(self, cmd, timeout=None):
            return ""

        def run_streaming(self):
            return ""

    with patch("pycastle.container_runner.ContainerRunner", _CapturingRunner):
        _run(run_agent("Test", prompt, tmp_path, {}, model="claude-sonnet-4-6"))

    assert captured["model"] == "claude-sonnet-4-6"


def test_run_agent_passes_effort_to_container_runner(tmp_path):
    """run_agent with effort kwarg must pass effort to ContainerRunner."""
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    captured: dict = {}

    class _CapturingRunner:
        def __init__(self, *args, model="", effort="", **kwargs):
            self.branch = None
            self.env = {}
            captured["model"] = model
            captured["effort"] = effort

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def exec_simple(self, cmd, timeout=None):
            return ""

        def run_streaming(self):
            return ""

    with patch("pycastle.container_runner.ContainerRunner", _CapturingRunner):
        _run(run_agent("Test", prompt, tmp_path, {}, effort="high"))

    assert captured["effort"] == "high"


def test_run_agent_defaults_model_and_effort_to_empty_string(tmp_path):
    """run_agent with no model/effort kwargs must pass empty strings."""
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    captured: dict = {}

    class _CapturingRunner:
        def __init__(self, *args, model="UNSET", effort="UNSET", **kwargs):
            self.branch = None
            self.env = {}
            captured["model"] = model
            captured["effort"] = effort

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def exec_simple(self, cmd, timeout=None):
            return ""

        def run_streaming(self):
            return ""

    with patch("pycastle.container_runner.ContainerRunner", _CapturingRunner):
        _run(run_agent("Test", prompt, tmp_path, {}))

    assert captured["model"] == ""
    assert captured["effort"] == ""


def test_run_streaming_includes_model_flag_when_set(tmp_path):
    """run_streaming must pass --model to exec_run when model is set on runner."""
    runner = _streaming_runner("Agent", [b"done\n"], tmp_path / "test.log")
    runner.model = "claude-sonnet-4-6"
    runner.effort = ""
    runner.write_file = MagicMock()
    runner.run_streaming()

    streaming_cmd = runner._container.exec_run.call_args_list[0][0][0][2]
    assert "--model claude-sonnet-4-6" in streaming_cmd


def test_run_streaming_includes_effort_flag_when_set(tmp_path):
    """run_streaming must pass --effort to exec_run when effort is set on runner."""
    runner = _streaming_runner("Agent", [b"done\n"], tmp_path / "test.log")
    runner.model = ""
    runner.effort = "high"
    runner.write_file = MagicMock()
    runner.run_streaming()

    streaming_cmd = runner._container.exec_run.call_args_list[0][0][0][2]
    assert "--effort high" in streaming_cmd


# ── Cycle 36-1: _build_claude_command includes required flags ────────────────


def test_build_claude_command_includes_output_format_stream_json():
    cmd = _build_claude_command()
    assert "--output-format stream-json" in cmd


def test_build_claude_command_includes_dangerously_skip_permissions():
    cmd = _build_claude_command()
    assert "--dangerously-skip-permissions" in cmd


def test_build_claude_command_includes_verbose():
    cmd = _build_claude_command()
    assert "--verbose" in cmd


# ── Issue 79: --print flag removed to prevent duplicate console output ────────


def test_build_claude_command_does_not_include_print_flag():
    cmd = _build_claude_command()
    assert "--print" not in cmd


def test_build_claude_command_includes_stdin_flag():
    cmd = _build_claude_command()
    assert "-p -" in cmd


# ── Cycle 36-2: stdin redirect from temp file, no heredoc ────────────────────


def test_build_claude_command_redirects_stdin_from_temp_file():
    cmd = _build_claude_command()
    assert "< /tmp/.pycastle_prompt" in cmd


def test_build_claude_command_does_not_use_temp_file():
    cmd = _build_claude_command()
    assert "/tmp/prompt.md" not in cmd


# ── Cycle 44-1: command string does not embed large prompt inline ─────────────


def test_build_claude_command_does_not_embed_large_prompt():
    cmd = _build_claude_command()
    assert len(cmd) < 1024


# ── Cycle 36-3: run_streaming passes correct command to exec_run ─────────────


def test_run_streaming_command_includes_required_flags(tmp_path):
    runner = _streaming_runner("TestAgent", [b"output\n"], tmp_path / "test.log")
    runner._prompt = "test prompt"
    runner.write_file = MagicMock()
    runner.run_streaming()
    streaming_cmd = runner._container.exec_run.call_args_list[0][0][0][2]
    assert "--output-format stream-json" in streaming_cmd
    assert "--dangerously-skip-permissions" in streaming_cmd
    assert "--verbose" in streaming_cmd
    assert "-p -" in streaming_cmd
    assert "< /tmp/.pycastle_prompt" in streaming_cmd


# ── Cycle 44-2: run_streaming writes prompt to temp file ─────────────────────


def test_run_streaming_writes_prompt_to_temp_file(tmp_path):
    runner = _streaming_runner("Agent", [b"output\n"], tmp_path / "test.log")
    runner._prompt = "my test prompt"
    runner.write_file = MagicMock()
    runner.run_streaming()
    runner.write_file.assert_called_once_with("my test prompt", "/tmp/.pycastle_prompt")


# ── Cycle 44-3: command string redirects stdin from temp file ─────────────────


def test_run_streaming_command_redirects_stdin_from_temp_file(tmp_path):
    runner = _streaming_runner("Agent", [b"output\n"], tmp_path / "test.log")
    runner._prompt = "test"
    runner.write_file = MagicMock()
    runner.run_streaming()
    streaming_cmd = runner._container.exec_run.call_args_list[0][0][0][2]
    assert "< /tmp/.pycastle_prompt" in streaming_cmd


# ── Cycle 44-4: temp prompt file is cleaned up after run ─────────────────────


def test_run_streaming_cleans_up_temp_prompt_file(tmp_path):
    runner = _streaming_runner("Agent", [b"output\n"], tmp_path / "test.log")
    runner._prompt = "test"
    runner.write_file = MagicMock()
    runner.run_streaming()
    all_cmds = [call[0][0] for call in runner._container.exec_run.call_args_list]
    assert any("rm -f /tmp/.pycastle_prompt" in " ".join(cmd) for cmd in all_cmds)


# ── Cycle 36-4: _prepare stores prompt on runner, no write_file ─────────────


def test_prepare_stores_prompt_on_runner(tmp_path):
    from pycastle.container_runner import _prepare

    prompt_file = tmp_path / "p.md"
    prompt_file.write_text("my prompt content")

    runner = MagicMock()
    runner.exec_simple.return_value = ""

    async def _run():
        await _prepare("test", runner, asyncio.get_event_loop(), None, prompt_file, {})

    asyncio.run(_run())

    assert runner._prompt == "my prompt content"


def test_prepare_does_not_call_write_file(tmp_path):
    from pycastle.container_runner import _prepare

    prompt_file = tmp_path / "p.md"
    prompt_file.write_text("my prompt content")

    runner = MagicMock()
    runner.exec_simple.return_value = ""

    async def _run():
        await _prepare("test", runner, asyncio.get_event_loop(), None, prompt_file, {})

    asyncio.run(_run())

    runner.write_file.assert_not_called()


# ── Cycle 36-5: streaming consumer prints each line immediately ──────────────


def test_run_streaming_prints_lines_from_separate_chunks(tmp_path, capsys):
    """Lines arriving in separate chunks must each be printed, not buffered until the end."""
    runner = _streaming_runner(
        "Bot", [b"line one\n", b"line two\n"], tmp_path / "test.log"
    )
    runner.run_streaming()
    out = capsys.readouterr().out
    assert "[Bot] line one" in out
    assert "[Bot] line two" in out


# ── Cycle 37-1: parent .git mounted rw at /.pycastle-parent-git ──────────────


@patch("pycastle.container_runner.LOGS_DIR")
@patch("pycastle.container_runner.docker")
def test_container_mounts_parent_git_rw(mock_docker, mock_logs_dir, tmp_path):
    """When worktree_host_path is set, <mount_path>/.git must be bound at /.pycastle-parent-git with mode rw."""
    mock_container = MagicMock()
    mock_docker.from_env.return_value.containers.run.return_value = mock_container

    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    runner = ContainerRunner(
        "test", tmp_path, {}, branch="feature/test", worktree_host_path=worktree_path
    )
    runner.__enter__()
    runner.__exit__(None, None, None)

    volumes = mock_docker.from_env.return_value.containers.run.call_args.kwargs[
        "volumes"
    ]
    expected_host = str((tmp_path / ".git").resolve()).replace("\\", "/")
    assert "/.pycastle-parent-git" in {v["bind"] for v in volumes.values()}, (
        f"/.pycastle-parent-git not mounted; volumes={volumes}"
    )
    parent_git_entry = next(
        v for v in volumes.values() if v["bind"] == "/.pycastle-parent-git"
    )
    assert parent_git_entry["mode"] == "rw", (
        f"/.pycastle-parent-git must be rw; got mode={parent_git_entry['mode']!r}"
    )
    host_key = next(
        k for k, v in volumes.items() if v["bind"] == "/.pycastle-parent-git"
    )
    assert host_key == expected_host, (
        f"Wrong host path for /.pycastle-parent-git: {host_key!r}, expected {expected_host!r}"
    )


# ── Cycle 36-6: run_agent completes without .claude/settings.json ────────────


def test_run_agent_does_not_write_claude_settings_json(tmp_path):
    """--dangerously-skip-permissions makes pre-creating .claude/settings.json unnecessary."""
    prompt = tmp_path / "p.md"
    prompt.write_text("test prompt")

    written_paths: list[str] = []

    class _TrackingRunner:
        def __init__(self, *_, **__):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def exec_simple(self, cmd, timeout=None):
            return ""

        def write_file(self, content, path):
            written_paths.append(path)

        def run_streaming(self):
            return "done"

    with patch("pycastle.container_runner.ContainerRunner", _TrackingRunner):
        _run(run_agent("test", prompt, tmp_path, {}))

    assert not any(".claude/settings.json" in p for p in written_paths), (
        f"settings.json must not be pre-created; written paths: {written_paths}"
    )


# ── Cycle 50-1: PREFLIGHT_CHECKS and IMPLEMENT_CHECKS in defaults/config ─────


def test_preflight_checks_contains_ruff_mypy_pytest():
    from pycastle.defaults.config import PREFLIGHT_CHECKS

    names = [name for name, _ in PREFLIGHT_CHECKS]
    assert names == ["ruff", "mypy", "pytest"]


def test_preflight_checks_commands():
    from pycastle.defaults.config import PREFLIGHT_CHECKS

    cmds = {name: cmd for name, cmd in PREFLIGHT_CHECKS}
    assert cmds["ruff"] == "ruff check ."
    assert cmds["mypy"] == "mypy ."
    assert cmds["pytest"] == "pytest"


def test_implement_checks_contains_expected_commands():
    from pycastle.defaults.config import IMPLEMENT_CHECKS

    assert IMPLEMENT_CHECKS == [
        "ruff check --fix",
        "ruff format --check",
        "mypy .",
        "pytest",
    ]


# ── Cycle 50-2: _preflight() runs all checks independently ───────────────────


def _make_preflight_runner(results: dict[str, str | Exception]):
    """Fake runner whose exec_simple returns or raises based on the command."""

    class _Runner:
        def __init__(self):
            self.branch = None
            self.env = {}

        def exec_simple(self, cmd, timeout=None):
            for key, val in results.items():
                if key in cmd:
                    if isinstance(val, Exception):
                        raise val
                    return val
            return ""

    return _Runner()


def test_preflight_all_checks_run_when_one_fails():
    """A DockerError in one check must not prevent the remaining checks from running."""
    from pycastle.container_runner import _preflight
    from pycastle.errors import DockerError

    ran: list[str] = []

    class _TrackingRunner:
        def __init__(self):
            self.branch = None
            self.env = {}

        def exec_simple(self, cmd, timeout=None):
            ran.append(cmd)
            if "ruff" in cmd:
                raise DockerError("ruff failed")
            return ""

    async def _coro():
        loop = asyncio.get_event_loop()
        checks = [("ruff", "ruff check ."), ("mypy", "mypy ."), ("pytest", "pytest")]
        return await _preflight("test", _TrackingRunner(), loop, None, checks)

    asyncio.run(_coro())
    assert len(ran) == 3


def test_preflight_returns_failure_tuples():
    from pycastle.container_runner import _preflight
    from pycastle.errors import DockerError

    async def _run():
        loop = asyncio.get_event_loop()
        checks = [("ruff", "ruff check ."), ("mypy", "mypy .")]
        runner = _make_preflight_runner(
            {"ruff check": DockerError("E501 line too long"), "mypy": ""}
        )
        return await _preflight("test", runner, loop, None, checks)

    failures = asyncio.run(_run())
    assert len(failures) == 1
    name, cmd, output = failures[0]
    assert name == "ruff"
    assert cmd == "ruff check ."
    assert "E501" in output


def test_preflight_returns_empty_list_on_clean_pass():
    from pycastle.container_runner import _preflight

    async def _run():
        loop = asyncio.get_event_loop()
        checks = [("ruff", "ruff check ."), ("mypy", "mypy ."), ("pytest", "pytest")]
        runner = _make_preflight_runner({})
        return await _preflight("test", runner, loop, None, checks)

    assert asyncio.run(_run()) == []


# ── Cycle 50-3: run_agent wires preflight, raises PreflightError, skip flag ───


class _PreflightFailRunner:
    """Fake runner whose exec_simple fails for ruff check."""

    def __init__(self, *args, **kwargs):
        self.branch = None
        self.env = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def exec_simple(self, cmd, timeout=None):
        from pycastle.errors import DockerError

        if "ruff check" in cmd:
            raise DockerError("E501 line too long")
        return ""

    def run_streaming(self):
        return ""


def test_run_agent_raises_preflight_error_when_check_fails(tmp_path):
    from pycastle.errors import PreflightError

    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    with (
        patch("pycastle.container_runner.ContainerRunner", _PreflightFailRunner),
        pytest.raises(PreflightError) as exc_info,
    ):
        _run(run_agent("Test", prompt, tmp_path, {}))

    assert len(exc_info.value.failures) >= 1


def test_run_agent_preflight_error_carries_correct_tuple(tmp_path):
    from pycastle.errors import PreflightError

    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    with (
        patch("pycastle.container_runner.ContainerRunner", _PreflightFailRunner),
        pytest.raises(PreflightError) as exc_info,
    ):
        _run(run_agent("Test", prompt, tmp_path, {}))

    name, cmd, output = exc_info.value.failures[0]
    assert name == "ruff"
    assert cmd == "ruff check ."
    assert "E501" in output


def test_run_agent_skip_preflight_bypasses_phase(tmp_path):
    """run_agent(skip_preflight=True) must proceed to Prepare and Work without pre-flight."""
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    with patch("pycastle.container_runner.ContainerRunner", _PreflightFailRunner):
        result = _run(run_agent("Test", prompt, tmp_path, {}, skip_preflight=True))

    assert result == ""


def test_run_agent_logs_preflight_phase(tmp_path, capsys):
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    with patch("pycastle.container_runner.ContainerRunner", _PhaseLogRunner):
        _run(run_agent("Test", prompt, tmp_path, {}))

    assert "[Test] Phase: Pre-flight" in capsys.readouterr().out


def test_prepare_runs_before_preflight(tmp_path, capsys):
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    with patch("pycastle.container_runner.ContainerRunner", _PhaseLogRunner):
        _run(run_agent("Test", prompt, tmp_path, {}))

    out = capsys.readouterr().out
    assert out.index("[Test] Phase: Prepare") < out.index("[Test] Phase: Pre-flight")


# ── Cycle 51-1: no agents spawned on preflight failure ───────────────────────


def test_no_agent_spawned_when_single_preflight_check_fails(tmp_path):
    """When preflight returns one failure, run_agent must NOT spawn any agent."""
    from pycastle.errors import PreflightError

    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    with (
        patch("pycastle.container_runner.ContainerRunner", _PreflightFailRunner),
        patch("pycastle.container_runner.run_agent") as mock_spawn,
        pytest.raises(PreflightError),
    ):
        _run(run_agent("Test", prompt, tmp_path, {}))

    mock_spawn.assert_not_called()


def test_no_agent_spawned_when_multiple_preflight_checks_fail(tmp_path):
    """When preflight returns multiple failures, run_agent must still not spawn any agent."""
    from pycastle.errors import DockerError, PreflightError

    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    class _TwoFailureRunner:
        def __init__(self, *args, **kwargs):
            self.branch = None
            self.env = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def exec_simple(self, cmd, timeout=None):
            if "ruff" in cmd or "mypy" in cmd:
                raise DockerError(f"{cmd} failed")
            return ""

        def run_streaming(self):
            return ""

    with (
        patch("pycastle.container_runner.ContainerRunner", _TwoFailureRunner),
        patch("pycastle.container_runner.run_agent") as mock_spawn,
        pytest.raises(PreflightError),
    ):
        _run(run_agent("Test", prompt, tmp_path, {}))

    mock_spawn.assert_not_called()


# ── Cycle 65-1: assistant text content extracted for terminal ─────────────────


def test_format_stream_line_extracts_text_from_assistant_message():
    line = '{"type":"assistant","message":{"id":"msg_x","content":[{"type":"text","text":"Analysing issues"}]}}'
    assert _format_stream_line(line) == "Analysing issues"


# ── Cycle 65-2: system init lines suppressed ──────────────────────────────────


def test_format_stream_line_returns_none_for_system_init():
    line = '{"type":"system","subtype":"init","session_id":"abc","tools":[]}'
    assert _format_stream_line(line) is None


# ── Cycle 65-3: tool_use content block summarised ─────────────────────────────


def test_format_stream_line_summarises_tool_use_block():
    line = '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1","name":"Bash","input":{}}]}}'
    assert _format_stream_line(line) == "(tool: Bash)"


# ── Cycle 65-4: result line prints result text ────────────────────────────────


def test_format_stream_line_returns_result_text():
    line = '{"type":"result","result":"Final answer here","session_id":"abc"}'
    assert _format_stream_line(line) == "Final answer here"


def test_format_stream_line_returns_none_for_empty_result():
    line = '{"type":"result","result":"","session_id":"abc"}'
    assert _format_stream_line(line) is None


def test_format_stream_line_returns_none_for_missing_result_key():
    line = '{"type":"result","session_id":"abc"}'
    assert _format_stream_line(line) is None


# ── Cycle 65-5: non-JSON line returned verbatim ───────────────────────────────


def test_format_stream_line_returns_plain_text_verbatim():
    assert _format_stream_line("plain text output") == "plain text output"


# ── Cycle 65-6: run_streaming terminal output is human-readable ──────────────


def test_run_streaming_terminal_shows_text_not_raw_json(tmp_path, capsys):
    """Terminal must show extracted text, not the raw JSON envelope."""
    json_line = b'{"type":"assistant","message":{"content":[{"type":"text","text":"Working on it"}]}}\n'
    runner = _streaming_runner("Planner", [json_line], tmp_path / "test.log")
    runner.run_streaming()
    out = capsys.readouterr().out
    assert "Working on it" in out
    assert '"type":"assistant"' not in out


def test_run_streaming_suppresses_system_init_line(tmp_path, capsys):
    """System init JSON must produce no terminal output at all."""
    json_line = b'{"type":"system","subtype":"init","session_id":"s1","tools":[]}\n'
    runner = _streaming_runner("Planner", [json_line], tmp_path / "test.log")
    runner.run_streaming()
    out = capsys.readouterr().out
    assert out == ""


def test_run_streaming_log_file_unchanged_for_json_lines(tmp_path):
    """Log file must still contain the raw, unmodified JSON bytes."""
    raw = b'{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n'
    log_path = tmp_path / "test.log"
    runner = _streaming_runner("Planner", [raw], log_path)
    runner.run_streaming()
    assert log_path.read_bytes() == raw


# ── Cycle 65-7: _format_stream_line edge cases ───────────────────────────────


def test_format_stream_line_joins_text_and_tool_use_with_space():
    line = '{"type":"assistant","message":{"content":[{"type":"text","text":"Reading files"},{"type":"tool_use","name":"Read","id":"t1","input":{}}]}}'
    assert _format_stream_line(line) == "Reading files (tool: Read)"


def test_format_stream_line_returns_none_for_whitespace_only_text():
    line = '{"type":"assistant","message":{"content":[{"type":"text","text":"   "}]}}'
    assert _format_stream_line(line) is None


def test_format_stream_line_returns_none_for_empty_content_list():
    line = '{"type":"assistant","message":{"content":[]}}'
    assert _format_stream_line(line) is None


def test_format_stream_line_returns_none_for_missing_message():
    line = '{"type":"assistant"}'
    assert _format_stream_line(line) is None


def test_format_stream_line_returns_none_for_null_message():
    line = '{"type":"assistant","message":null}'
    assert _format_stream_line(line) is None


def test_format_stream_line_returns_none_for_empty_result_string():
    line = '{"type":"result","result":""}'
    assert _format_stream_line(line) is None


def test_format_stream_line_returns_none_for_unknown_type():
    line = '{"type":"tool_result","content":"output"}'
    assert _format_stream_line(line) is None


def test_format_stream_line_returns_verbatim_for_json_array():
    line = '["not","a","dict"]'
    assert _format_stream_line(line) == line


# ── Issue 100: stage parameter in run_agent ───────────────────────────────────


def test_run_agent_accepts_stage_parameter(tmp_path):
    """run_agent must accept a stage keyword argument without raising."""
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    with patch("pycastle.container_runner.ContainerRunner", _PhaseLogRunner):
        result = _run(run_agent("Test", prompt, tmp_path, {}, stage="pre-planning"))

    assert result == ""
