import asyncio
import dataclasses
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pycastle.config import Config
from pycastle.container_runner import (
    ContainerRunner,
    _build_claude_command,
)
from pycastle.errors import AgentTimeoutError, UsageLimitError
from pycastle.services import GitService
from pycastle.iteration._deps import RecordingStatusDisplay


# ── Issue 153: docker_client injection ───────────────────────────────────────


def test_container_runner_init_uses_injected_docker_client():
    """ContainerRunner must accept docker_client and use it instead of docker.from_env()."""
    mock_client = MagicMock()
    runner = ContainerRunner(
        "test", Path("/fake"), {}, docker_client=mock_client, cfg=Config()
    )
    assert runner._client is mock_client


def test_container_runner_init_calls_docker_from_env_when_no_client_given():
    """When docker_client is None, __init__ must call docker.from_env()."""
    with patch("pycastle.container_runner.docker") as mock_docker:
        runner = ContainerRunner("test", Path("/fake"), {}, cfg=Config())
    assert runner._client is mock_docker.from_env.return_value


# ── helpers ──────────────────────────────────────────────────────────────────


def _streaming_runner(name: str, chunks: list, tmp_path: Path) -> ContainerRunner:
    """ContainerRunner whose run_streaming replays the given byte chunks."""
    mock_client = MagicMock()
    mock_result = MagicMock()
    mock_result.output = iter(chunks)
    mock_client.containers.run.return_value.exec_run.return_value = mock_result
    runner = ContainerRunner(
        name,
        Path("/fake"),
        {},
        docker_client=mock_client,
        cfg=Config(logs_dir=tmp_path),
    )
    runner.__enter__()
    return runner


def _fake_runner(exit_code=0, stdout=b"", stderr=b"", cfg=None):
    """ContainerRunner with mocked Docker container."""
    mock_client = MagicMock()
    mock_result = MagicMock()
    mock_result.exit_code = exit_code
    mock_result.output = (stdout, stderr)
    mock_client.containers.run.return_value.exec_run.return_value = mock_result
    if cfg is not None:
        effective_cfg = dataclasses.replace(cfg, logs_dir=Path(tempfile.mkdtemp()))
    else:
        effective_cfg = Config(logs_dir=Path(tempfile.mkdtemp()))
    runner = ContainerRunner(
        "test", Path("/fake"), {}, docker_client=mock_client, cfg=effective_cfg
    )
    runner.__enter__()
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


# ── Cycle 15: worktree add must not run inside the container ─────────────────


@patch("pycastle.container_runner.docker")
def test_worktree_add_not_called_inside_container(mock_docker, tmp_path):
    """When worktree_host_path is provided, ContainerRunner must not exec worktree add."""
    mock_container = MagicMock()
    mock_docker.from_env.return_value.containers.run.return_value = mock_container
    mock_container.exec_run.return_value = MagicMock(exit_code=0, output=b"")

    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    runner = ContainerRunner(
        "test",
        tmp_path,
        {},
        branch="feature/test",
        worktree_host_path=worktree_path,
        cfg=Config(logs_dir=tmp_path / "logs"),
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


@patch("pycastle.container_runner.docker")
def test_implementer_mounts_worktree_at_workspace(mock_docker, tmp_path):
    """When worktree_host_path is provided the container must bind it at /home/agent/workspace."""
    mock_container = MagicMock()
    mock_docker.from_env.return_value.containers.run.return_value = mock_container
    mock_container.exec_run.return_value = MagicMock(exit_code=0, output=b"")

    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    runner = ContainerRunner(
        "test",
        tmp_path,
        {},
        branch="feature/test",
        worktree_host_path=worktree_path,
        cfg=Config(logs_dir=tmp_path / "logs"),
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


@patch("pycastle.container_runner.docker")
def test_container_mounts_gitdir_overlay_at_workspace_git(mock_docker, tmp_path):
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
        cfg=Config(logs_dir=tmp_path / "logs"),
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


# ── Cycle 4: exec_simple raises TimeoutError on stalled command ───────────────


def test_exec_simple_times_out():
    blocker = threading.Event()
    mock_client = MagicMock()
    mock_client.containers.run.return_value.exec_run.side_effect = lambda *a, **kw: (
        blocker.wait() or None
    )
    runner = ContainerRunner(
        "test",
        Path("/fake"),
        {},
        docker_client=mock_client,
        cfg=Config(logs_dir=Path(tempfile.mkdtemp())),
    )
    runner.__enter__()

    try:
        with pytest.raises(TimeoutError):
            runner.exec_simple("sleep inf", timeout=0.05)
    finally:
        blocker.set()  # release the background thread


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


def test_setup_configures_git_identity_with_readonly_repo_mount():
    """_setup must use --global because the repo is mounted read-only inside the container."""
    from pycastle.container_runner import _setup

    _Runner, exec_log = _make_exec_logging_runner()
    mock_git = _make_git_service()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            _setup("test", _Runner(), loop, 30.0, git_service=mock_git)
        )
    finally:
        loop.close()

    assert any("git config --global user.name" in cmd for cmd in exec_log)
    assert any("git config --global user.email" in cmd for cmd in exec_log)


# ── Cycle 23-4: run_streaming raises AgentTimeoutError on idle timeout ────────


def _never_yields():
    """Generator that blocks forever without yielding — simulates a hung agent."""
    event = threading.Event()
    event.wait()
    return
    yield  # makes this a generator


def test_run_streaming_raises_agent_timeout_error_when_idle(tmp_path):
    mock_client = MagicMock()
    mock_result = MagicMock()
    mock_result.output = _never_yields()
    mock_client.containers.run.return_value.exec_run.return_value = mock_result
    runner = ContainerRunner(
        "test",
        Path("/fake"),
        {},
        docker_client=mock_client,
        cfg=Config(logs_dir=tmp_path, idle_timeout=0.05),
    )
    runner.__enter__()

    with pytest.raises(AgentTimeoutError):
        runner.run_streaming()


# ── Issue 310: run_streaming produces no stdout ──────────────────────────────


def test_run_streaming_produces_no_stdout(tmp_path, capsys):
    json_line = b'{"type":"assistant","message":{"content":[{"type":"text","text":"Working on it"}]}}\n'
    runner = _streaming_runner("TestAgent", [json_line], tmp_path)
    runner.run_streaming()
    assert capsys.readouterr().out == ""


def test_run_streaming_produces_no_stdout_for_plain_text(tmp_path, capsys):
    runner = _streaming_runner("Bot", [b"line one\nline two\n"], tmp_path)
    runner.run_streaming()
    assert capsys.readouterr().out == ""


# ── Cycle 24-A2: log file stays raw (unprefixed) ─────────────────────────────


def test_run_streaming_log_file_is_raw_unprefixed(tmp_path):
    runner = _streaming_runner("TestAgent", [b"hello world\n"], tmp_path)
    runner.run_streaming()
    assert runner._log_path.read_text() == "hello world\n"
    assert "[TestAgent]" not in runner._log_path.read_text()


def test_run_streaming_log_file_contains_full_raw_output(tmp_path):
    """Log file must capture all raw bytes, including multi-chunk output."""
    runner = _streaming_runner("Bot", [b"line one\n", b"line two\n"], tmp_path)
    runner.run_streaming()
    content = runner._log_path.read_text()
    assert content == "line one\nline two\n"


# ── Issue 339: run_streaming per-chunk reset_idle_timer ──────────────────────


def test_run_streaming_calls_reset_idle_timer_per_chunk(tmp_path):
    tool_chunk = b'{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","id":"t1","input":{}}]}}\n'
    runner = _streaming_runner("Bot", [tool_chunk, tool_chunk], tmp_path)
    display = RecordingStatusDisplay()

    runner.run_streaming(status_display=display)

    reset_calls = [c for c in display.calls if c[0] == "reset_idle_timer"]
    assert len(reset_calls) == 2
    assert all(c == ("reset_idle_timer", "Bot") for c in reset_calls)


def test_run_streaming_tool_use_only_line_resets_timer(tmp_path):
    tool_chunk = b'{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","id":"t1","input":{}}]}}\n'
    runner = _streaming_runner("Bot", [tool_chunk], tmp_path)
    display = RecordingStatusDisplay()

    runner.run_streaming(status_display=display)

    assert ("reset_idle_timer", "Bot") in display.calls


def test_run_streaming_system_line_resets_timer(tmp_path):
    system_chunk = b'{"type":"system","subtype":"init","session_id":"abc","tools":[]}\n'
    runner = _streaming_runner("Bot", [system_chunk], tmp_path)
    display = RecordingStatusDisplay()

    runner.run_streaming(status_display=display)

    assert ("reset_idle_timer", "Bot") in display.calls


def test_run_streaming_result_line_resets_timer(tmp_path):
    result_chunk = b'{"type":"result","result":"Final answer","session_id":"abc"}\n'
    runner = _streaming_runner("Bot", [result_chunk], tmp_path)
    display = RecordingStatusDisplay()

    runner.run_streaming(status_display=display)

    assert ("reset_idle_timer", "Bot") in display.calls


def test_run_streaming_partial_chunk_resets_timer(tmp_path):
    partial_chunk = b'{"type":"assistant","message":{"content":[{"type":"text","text":"no newline here"}'
    runner = _streaming_runner("Bot", [partial_chunk], tmp_path)
    display = RecordingStatusDisplay()

    runner.run_streaming(status_display=display)

    assert ("reset_idle_timer", "Bot") in display.calls


def test_run_streaming_single_chunk_with_multiple_lines_resets_timer_once(tmp_path):
    line_a = (
        b'{"type":"assistant","message":{"content":[{"type":"text","text":"Hello"}]}}\n'
    )
    line_b = (
        b'{"type":"assistant","message":{"content":[{"type":"text","text":"World"}]}}\n'
    )
    runner = _streaming_runner("Bot", [line_a + line_b], tmp_path)
    display = RecordingStatusDisplay()

    runner.run_streaming(status_display=display)

    reset_calls = [c for c in display.calls if c[0] == "reset_idle_timer"]
    assert len(reset_calls) == 1


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


def test_run_streaming_includes_model_flag_when_set(tmp_path):
    """run_streaming must pass --model to exec_run when model is set on runner."""
    runner = _streaming_runner("Agent", [b"done\n"], tmp_path)
    runner.model = "claude-sonnet-4-6"
    runner.effort = ""
    runner.run_streaming()

    streaming_cmd = runner._container.exec_run.call_args_list[0][0][0][2]
    assert "--model claude-sonnet-4-6" in streaming_cmd


def test_run_streaming_includes_effort_flag_when_set(tmp_path):
    """run_streaming must pass --effort to exec_run when effort is set on runner."""
    runner = _streaming_runner("Agent", [b"done\n"], tmp_path)
    runner.model = ""
    runner.effort = "high"
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


# ── Cycle 37-1: parent .git mounted rw at /.pycastle-parent-git ──────────────


@patch("pycastle.container_runner.docker")
def test_container_mounts_parent_git_rw(mock_docker, tmp_path):
    """When worktree_host_path is set, <mount_path>/.git must be bound at /.pycastle-parent-git with mode rw."""
    mock_container = MagicMock()
    mock_docker.from_env.return_value.containers.run.return_value = mock_container

    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    runner = ContainerRunner(
        "test",
        tmp_path,
        {},
        branch="feature/test",
        worktree_host_path=worktree_path,
        cfg=Config(logs_dir=tmp_path / "logs"),
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


# ── Cycle 50-1: PREFLIGHT_CHECKS and IMPLEMENT_CHECKS in defaults/config ─────


def test_preflight_checks_contains_ruff_mypy_pytest():
    from pycastle.config import Config

    names = [name for name, _ in Config().preflight_checks]
    assert names == ["ruff", "mypy", "pytest"]


def test_preflight_checks_commands():
    from pycastle.config import Config

    cmds = {name: cmd for name, cmd in Config().preflight_checks}
    assert cmds["ruff"] == "ruff check ."
    assert cmds["mypy"] == "mypy ."
    assert cmds["pytest"] == "pytest"


def test_implement_checks_contains_expected_commands():
    from pycastle.config import Config

    assert Config().implement_checks == (
        "ruff check --fix",
        "ruff format --check",
        "mypy .",
        "pytest",
    )


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


# ── Cycle 65-6: run_streaming writes raw log, suppresses all stdout ──────────


def test_run_streaming_suppresses_system_init_line(tmp_path, capsys):
    """System init JSON must produce no terminal output at all."""
    json_line = b'{"type":"system","subtype":"init","session_id":"s1","tools":[]}\n'
    runner = _streaming_runner("Planner", [json_line], tmp_path)
    runner.run_streaming()
    out = capsys.readouterr().out
    assert out == ""


def test_run_streaming_log_file_unchanged_for_json_lines(tmp_path):
    """Log file must still contain the raw, unmodified JSON bytes."""
    raw = b'{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n'
    runner = _streaming_runner("Planner", [raw], tmp_path)
    runner.run_streaming()
    assert runner._log_path.read_bytes() == raw


# ── Issue 180: UsageLimitError stream detection ───────────────────────────────


def test_run_streaming_raises_usage_limit_error_on_session_limit_line(tmp_path):
    runner = _streaming_runner(
        "Agent",
        [b"You've hit your session limit\n"],
        tmp_path,
    )
    with pytest.raises(UsageLimitError):
        runner.run_streaming()


def test_run_streaming_raises_usage_limit_error_case_insensitive(tmp_path):
    runner = _streaming_runner(
        "Agent",
        [b"you've hit your session limit\n"],
        tmp_path,
    )
    with pytest.raises(UsageLimitError):
        runner.run_streaming()


def test_run_streaming_raises_usage_limit_error_on_credit_balance_line(tmp_path):
    runner = _streaming_runner(
        "Agent",
        [b"Credit balance is too low for this request\n"],
        tmp_path,
    )
    with pytest.raises(UsageLimitError):
        runner.run_streaming()


def test_run_streaming_does_not_raise_for_normal_line(tmp_path):
    runner = _streaming_runner(
        "Agent",
        [b"All good, processing normally\n"],
        tmp_path,
    )
    runner.run_streaming()


def test_run_streaming_raises_when_pattern_split_across_chunks(tmp_path):
    runner = _streaming_runner(
        "Agent",
        [b"You've hit ", b"your weekly limit\n"],
        tmp_path,
    )
    with pytest.raises(UsageLimitError):
        runner.run_streaming()


def test_run_streaming_error_carries_original_casing(tmp_path):
    runner = _streaming_runner(
        "Agent",
        [b"YOU'VE HIT YOUR SESSION LIMIT\n"],
        tmp_path,
    )
    with pytest.raises(UsageLimitError) as exc_info:
        runner.run_streaming()
    assert str(exc_info.value) == "YOU'VE HIT YOUR SESSION LIMIT"


# ── Issue 186: UsageLimitError false positive in JSON tool-result lines ────────


def test_run_streaming_does_not_raise_for_json_line_containing_usage_limit_phrase(
    tmp_path,
):
    """A JSON-encoded tool result that mentions a usage-limit phrase is not a real limit."""
    import json

    json_line = json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "content": "You've hit your session limit is documented here as an example",
                    }
                ],
            },
        }
    )
    runner = _streaming_runner(
        "Agent",
        [(json_line + "\n").encode()],
        tmp_path,
    )
    runner.run_streaming()  # must not raise


# ── Issue 232: JSON result line with 429 not caught ───────────────────────────


def test_run_streaming_raises_usage_limit_error_on_json_result_with_429(tmp_path):
    """A JSON result line with api_error_status 429 must raise UsageLimitError."""
    import json

    json_line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "api_error_status": 429,
            "result": "You've hit your limit · resets 4:30pm (UTC)",
        }
    )
    runner = _streaming_runner(
        "Agent",
        [(json_line + "\n").encode()],
        tmp_path,
    )
    with pytest.raises(UsageLimitError):
        runner.run_streaming()


def test_run_streaming_raises_usage_limit_error_on_json_result_matching_pattern(
    tmp_path,
):
    """A JSON result line whose result field matches a usage_limit_pattern raises UsageLimitError."""
    import json

    json_line = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "api_error_status": 503,
            "result": "You've hit your session limit",
        }
    )
    runner = _streaming_runner(
        "Agent",
        [(json_line + "\n").encode()],
        tmp_path,
    )
    with pytest.raises(UsageLimitError):
        runner.run_streaming()


def test_run_streaming_does_not_raise_for_successful_json_result(tmp_path):
    """A normal JSON result line (no error) must not raise UsageLimitError."""
    import json

    json_line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Task completed successfully.",
        }
    )
    runner = _streaming_runner(
        "Agent",
        [(json_line + "\n").encode()],
        tmp_path,
    )
    runner.run_streaming()  # must not raise


def test_run_streaming_does_not_crash_on_json_result_with_null_result_field(tmp_path):
    """A JSON result error with result=null must not raise AttributeError or UsageLimitError."""
    import json

    json_line = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "api_error_status": 503,
            "result": None,
        }
    )
    runner = _streaming_runner(
        "Agent",
        [(json_line + "\n").encode()],
        tmp_path,
    )
    runner.run_streaming()  # must not raise


# ── Issue 203: cfg injection into ContainerRunner ─────────────────────────────


def test_container_runner_uses_custom_logs_dir_from_cfg(tmp_path):
    """ContainerRunner with cfg=Config(logs_dir=...) must set log_path under that dir."""
    custom_logs = tmp_path / "my_logs"
    runner = ContainerRunner(
        "my-task",
        Path("/fake"),
        {},
        docker_client=MagicMock(),
        cfg=Config(logs_dir=custom_logs),
    )
    assert runner.log_path.parent == custom_logs


def test_run_streaming_uses_usage_limit_patterns_from_cfg(tmp_path):
    """Custom usage_limit_patterns injected via cfg must trigger UsageLimitError."""
    mock_client = MagicMock()
    mock_result = MagicMock()
    mock_result.output = iter([b"CUSTOM_LIMIT_SENTINEL reached\n"])
    mock_client.containers.run.return_value.exec_run.return_value = mock_result
    runner = ContainerRunner(
        "test",
        Path("/fake"),
        {},
        docker_client=mock_client,
        cfg=Config(logs_dir=tmp_path, usage_limit_patterns=("CUSTOM_LIMIT_SENTINEL",)),
    )
    runner.__enter__()

    with pytest.raises(UsageLimitError):
        runner.run_streaming()


def test_run_streaming_default_patterns_not_triggered_by_custom_cfg(tmp_path):
    """Default usage_limit_patterns must not trigger when cfg overrides them."""
    mock_client = MagicMock()
    mock_result = MagicMock()
    # Default pattern "You've hit your" should NOT trigger with the custom cfg
    mock_result.output = iter([b"You've hit your session limit\n"])
    mock_client.containers.run.return_value.exec_run.return_value = mock_result
    runner = ContainerRunner(
        "test",
        Path("/fake"),
        {},
        docker_client=mock_client,
        cfg=Config(logs_dir=tmp_path, usage_limit_patterns=("CUSTOM_LIMIT_SENTINEL",)),
    )
    runner.__enter__()

    result = runner.run_streaming()
    assert "You've hit your session limit" in result


# ── Issue 310: StatusDisplay lifecycle wiring ─────────────────────────────────


def _unstarted_runner(name: str, tmp_path: Path) -> ContainerRunner:
    """ContainerRunner with mocked Docker whose __enter__ has NOT been called yet."""
    mock_client = MagicMock()
    mock_exec = MagicMock()
    mock_exec.exit_code = 0
    mock_exec.output = (b"", b"")
    mock_client.containers.run.return_value.exec_run.return_value = mock_exec
    return ContainerRunner(
        name,
        Path("/fake"),
        {},
        docker_client=mock_client,
        cfg=Config(logs_dir=tmp_path),
    )


def test_setup_calls_add_agent_with_name_and_phase(tmp_path):
    from pycastle.container_runner import _setup
    from pycastle.iteration._deps import RecordingStatusDisplay

    runner = _unstarted_runner("implementer-42", tmp_path)
    display = RecordingStatusDisplay()

    async def _run():
        loop = asyncio.get_event_loop()
        await _setup(
            "implementer-42",
            runner,
            loop,
            None,
            git_service=_make_git_service(),
            status_display=display,
        )

    asyncio.run(_run())

    add_calls = [c for c in display.calls if c[0] == "add_agent"]
    assert len(add_calls) == 1
    assert add_calls[0][1] == "implementer-42"
    assert add_calls[0][2] == "Setup"


def test_work_calls_update_phase_work(tmp_path):
    from pycastle.container_runner import _work
    from pycastle.iteration._deps import RecordingStatusDisplay

    runner = _streaming_runner("implementer-42", [b"done\n"], tmp_path)
    display = RecordingStatusDisplay()

    async def _run():
        loop = asyncio.get_event_loop()
        await _work("implementer-42", runner, loop, status_display=display)

    asyncio.run(_run())
    assert ("update_phase", "implementer-42", "Work") in display.calls


def test_work_produces_no_stdout(tmp_path, capsys):
    from pycastle.container_runner import _work
    from pycastle.iteration._deps import RecordingStatusDisplay

    json_line = b'{"type":"assistant","message":{"content":[{"type":"text","text":"Working"}]}}\n'
    runner = _streaming_runner("implementer-42", [json_line], tmp_path)

    async def _run():
        loop = asyncio.get_event_loop()
        await _work(
            "implementer-42", runner, loop, status_display=RecordingStatusDisplay()
        )

    asyncio.run(_run())
    assert capsys.readouterr().out == ""


def test_preflight_calls_update_phase_preflight(tmp_path):
    from pycastle.container_runner import _preflight
    from pycastle.iteration._deps import RecordingStatusDisplay

    display = RecordingStatusDisplay()

    async def _run():
        loop = asyncio.get_event_loop()
        runner = _make_preflight_runner({})
        return await _preflight(
            "agent-1", runner, loop, None, [], status_display=display
        )

    asyncio.run(_run())
    assert ("update_phase", "agent-1", "Pre-flight") in display.calls


# ── Issue 337: pip install failure must propagate from _setup ─────────────────


def test_setup_propagates_docker_error_when_pip_install_fails():
    """_setup must not swallow DockerError from a failed pip install."""
    from pycastle.container_runner import _setup
    from pycastle.errors import DockerError

    class _FailingPipRunner:
        def __init__(self):
            self.branch = None
            self.env = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def exec_simple(self, cmd, timeout=None):
            if "pip install" in cmd:
                raise DockerError("pip install failed: exit 1")
            return ""

    async def _run():
        loop = asyncio.get_event_loop()
        await _setup(
            "test",
            _FailingPipRunner(),
            loop,
            30.0,
            git_service=_make_git_service(),
        )

    with pytest.raises(DockerError, match="pip install failed"):
        asyncio.run(_run())


# ── Issue 344: Docker client connection leak on shutdown ──────────────────────


def test_exit_closes_client_when_runner_owns_it(tmp_path):
    """When no docker_client is injected, __exit__ must close the created client."""
    with patch("pycastle.container_runner.docker") as mock_docker:
        mock_container = MagicMock()
        mock_docker.from_env.return_value.containers.run.return_value = mock_container
        runner = ContainerRunner(
            "test", Path("/fake"), {}, cfg=Config(logs_dir=tmp_path)
        )
        runner.__enter__()
        runner.__exit__(None, None, None)
    mock_docker.from_env.return_value.close.assert_called_once()


def test_exit_does_not_close_injected_client(tmp_path):
    """When docker_client is injected, __exit__ must not close it."""
    mock_client = MagicMock()
    runner = ContainerRunner(
        "test",
        Path("/fake"),
        {},
        docker_client=mock_client,
        cfg=Config(logs_dir=tmp_path),
    )
    runner.__enter__()
    runner.__exit__(None, None, None)
    mock_client.close.assert_not_called()


def test_exit_swallows_close_exception():
    """Exceptions from client.close() must not propagate out of __exit__."""
    with patch("pycastle.container_runner.docker") as mock_docker:
        mock_docker.from_env.return_value.close.side_effect = RuntimeError(
            "connection reset"
        )
        runner = ContainerRunner("test", Path("/fake"), {}, cfg=Config())
        runner.__exit__(None, None, None)  # must not raise


# ── Issue 349: StreamParser integration ──────────────────────────────────────


def test_run_streaming_in_work_phase_prints_complete_turn(tmp_path):
    json_line = b'{"type":"assistant","message":{"content":[{"type":"text","text":"Analysing issues"}]}}\n'
    runner = _streaming_runner("Implementer #1", [json_line], tmp_path)
    display = RecordingStatusDisplay()

    runner.run_streaming(display, print_output=True)

    print_calls = [c for c in display.calls if c[0] == "print"]
    assert len(print_calls) == 1
    assert print_calls[0][1].plain == "[Implementer #1] Analysing issues\n"


def test_run_streaming_without_print_output_does_not_call_print(tmp_path):
    json_line = b'{"type":"assistant","message":{"content":[{"type":"text","text":"Analysing issues"}]}}\n'
    runner = _streaming_runner("Bot", [json_line], tmp_path)
    display = RecordingStatusDisplay()

    runner.run_streaming(display, print_output=False)

    assert not any(c[0] == "print" for c in display.calls)


def test_run_streaming_tool_use_only_does_not_call_print_in_work_phase(tmp_path):
    tool_chunk = b'{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","id":"t1","input":{}}]}}\n'
    runner = _streaming_runner("Bot", [tool_chunk], tmp_path)
    display = RecordingStatusDisplay()

    runner.run_streaming(display, print_output=True)

    assert not any(c[0] == "print" for c in display.calls)


def test_work_calls_print_for_complete_assistant_turn(tmp_path):
    from pycastle.container_runner import _work

    json_line = b'{"type":"assistant","message":{"content":[{"type":"text","text":"Fixing bug"}]}}\n'
    runner = _streaming_runner("Implementer #3", [json_line], tmp_path)
    display = RecordingStatusDisplay()

    async def _run():
        loop = asyncio.get_event_loop()
        await _work("Implementer #3", runner, loop, status_display=display)

    asyncio.run(_run())

    print_calls = [c for c in display.calls if c[0] == "print"]
    assert len(print_calls) == 1
    assert print_calls[0][1].plain == "[Implementer #3] Fixing bug\n"


def test_work_does_not_call_print_for_tool_use_turns(tmp_path):
    from pycastle.container_runner import _work

    tool_chunk = b'{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","id":"t1","input":{}}]}}\n'
    runner = _streaming_runner("Implementer #3", [tool_chunk], tmp_path)
    display = RecordingStatusDisplay()

    async def _run():
        loop = asyncio.get_event_loop()
        await _work("Implementer #3", runner, loop, status_display=display)

    asyncio.run(_run())

    assert not any(c[0] == "print" for c in display.calls)


def test_run_streaming_with_print_output_still_calls_reset_idle_timer(tmp_path):
    json_line = b'{"type":"assistant","message":{"content":[{"type":"text","text":"Working"}]}}\n'
    runner = _streaming_runner("Bot", [json_line], tmp_path)
    display = RecordingStatusDisplay()

    runner.run_streaming(display, print_output=True)

    assert ("reset_idle_timer", "Bot") in display.calls


def test_run_streaming_multiple_turns_prints_each_one(tmp_path):
    line_a = b'{"type":"assistant","message":{"content":[{"type":"text","text":"First turn"}]}}\n'
    line_b = b'{"type":"assistant","message":{"content":[{"type":"text","text":"Second turn"}]}}\n'
    runner = _streaming_runner("Bot", [line_a + line_b], tmp_path)
    display = RecordingStatusDisplay()

    runner.run_streaming(display, print_output=True)

    print_calls = [c for c in display.calls if c[0] == "print"]
    assert len(print_calls) == 2
    assert print_calls[0][1].plain == "[Bot] First turn\n"
    assert print_calls[1][1].plain == "[Bot] Second turn\n"


# ── Issue 377: rich Text prefix with agent-name source ────────────────────────


def test_run_streaming_print_uses_rich_text_object_with_agent_name_source(tmp_path):
    from rich.text import Text

    json_line = b'{"type":"assistant","message":{"content":[{"type":"text","text":"Working"}]}}\n'
    runner = _streaming_runner("Implementer #1", [json_line], tmp_path)
    display = RecordingStatusDisplay()

    runner.run_streaming(display, print_output=True)

    print_calls = [c for c in display.calls if c[0] == "print"]
    assert len(print_calls) == 1
    assert isinstance(print_calls[0][1], Text)
    assert print_calls[0][2] == "Implementer #1"
