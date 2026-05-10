"""Tests for ContainerRunner using a fake DockerSession."""

import asyncio
import json
import threading
from pathlib import Path
from typing import cast

import pytest

from pycastle.agent_output_protocol import AgentRole, CommitMessageOutput
from pycastle.config import Config
from pycastle.container_runner import ContainerRunner, _build_claude_command
from pycastle.docker_session import DockerSession
from pycastle.errors import AgentTimeoutError, DockerError, UsageLimitError
from pycastle.iteration._deps import RecordingStatusDisplay
from pycastle.session_resume import RunKind

_ROLE = AgentRole.IMPLEMENTER

_COMPLETE_LINE = b'{"type":"result","result":"<commit_message>done</commit_message>","is_error":false}\n'


# ── Fake DockerSession ────────────────────────────────────────────────────────


class FakeDockerSession:
    """Minimal DockerSession test double — implements exec_simple, exec_stream, write_file."""

    def __init__(
        self,
        exec_handlers: dict[str, object] | None = None,
        stream_chunks: list[bytes] | None = None,
    ) -> None:
        self.entered = False
        self.exec_calls: list[str] = []
        self.write_calls: list[tuple[str, str]] = []
        self.stream_calls: list[str] = []
        self._exec_handlers = exec_handlers or {}
        self._stream_chunks = stream_chunks or [_COMPLETE_LINE]

    def __enter__(self) -> "FakeDockerSession":
        self.entered = True
        return self

    def __exit__(self, *_) -> None:
        pass

    def exec_simple(self, command: str, timeout: float | None = None) -> str:
        self.exec_calls.append(command)
        for needle, handler in self._exec_handlers.items():
            if needle in command:
                if isinstance(handler, BaseException):
                    raise handler
                if callable(handler):
                    return handler(command)
                return str(handler)
        return ""

    def exec_stream(self, command: str):
        self.stream_calls.append(command)
        return iter(self._stream_chunks)

    def write_file(self, content: str, container_path: str) -> None:
        self.write_calls.append((container_path, content))


def _make_runner(
    name: str = "agent",
    session: FakeDockerSession | None = None,
    status_display=None,
    cfg: Config | None = None,
    tmp_path: Path | None = None,
    model: str = "",
    effort: str = "",
) -> tuple[ContainerRunner, FakeDockerSession]:
    if session is None:
        session = FakeDockerSession()
    if cfg is None:
        cfg = Config(logs_dir=tmp_path or Path("/tmp/pycastle-tests"))
    runner = ContainerRunner(
        name,
        cast(DockerSession, session),
        model=model,
        effort=effort,
        status_display=status_display,
        cfg=cfg,
    )
    return runner, session


# ── _build_claude_command ────────────────────────────────────────────────────


def test_build_claude_command_includes_output_format_stream_json():
    assert "--output-format stream-json" in _build_claude_command()


def test_build_claude_command_includes_dangerously_skip_permissions():
    assert "--dangerously-skip-permissions" in _build_claude_command()


def test_build_claude_command_includes_verbose():
    assert "--verbose" in _build_claude_command()


def test_build_claude_command_includes_stdin_redirect():
    assert "< /tmp/.pycastle_prompt" in _build_claude_command()


def test_build_claude_command_does_not_include_print_flag():
    assert "--print" not in _build_claude_command()


def test_build_claude_command_includes_model_when_set():
    assert "--model claude-opus-4-7" in _build_claude_command(model="claude-opus-4-7")


def test_build_claude_command_includes_effort_when_set():
    assert "--effort high" in _build_claude_command(effort="high")


def test_build_claude_command_excludes_flags_when_unset():
    cmd = _build_claude_command()
    assert "--model" not in cmd
    assert "--effort" not in cmd


def test_build_claude_command_includes_session_id_when_fresh_with_uuid():
    cmd = _build_claude_command(run_kind=RunKind.FRESH, session_uuid="abc-123")
    assert "--session-id abc-123" in cmd
    assert "--resume" not in cmd


def test_build_claude_command_includes_resume_when_resume_with_uuid():
    cmd = _build_claude_command(run_kind=RunKind.RESUME, session_uuid="abc-123")
    assert "--resume abc-123" in cmd
    assert "--session-id" not in cmd


def test_build_claude_command_excludes_session_flags_when_uuid_absent():
    cmd = _build_claude_command()
    assert "--session-id" not in cmd
    assert "--resume" not in cmd


# ── Constructor ──────────────────────────────────────────────────────────────


def test_container_runner_constructor_takes_session(tmp_path):
    session = FakeDockerSession()
    runner = ContainerRunner(
        "agent", cast(DockerSession, session), cfg=Config(logs_dir=tmp_path)
    )
    assert runner.name == "agent"
    assert runner.log_path.parent == tmp_path


def test_container_runner_does_not_expose_prepare_method(tmp_path):
    runner, _ = _make_runner(tmp_path=tmp_path)
    assert not hasattr(runner, "prepare")


def test_container_runner_does_not_expose_run_streaming_method(tmp_path):
    runner, _ = _make_runner(tmp_path=tmp_path)
    assert not hasattr(runner, "run_streaming")


def test_container_runner_does_not_expose_exec_simple_or_write_file(tmp_path):
    runner, _ = _make_runner(tmp_path=tmp_path)
    assert not hasattr(runner, "exec_simple")
    assert not hasattr(runner, "write_file")


# ── setup() ──────────────────────────────────────────────────────────────────


def test_setup_enters_session(tmp_path):
    runner, session = _make_runner(tmp_path=tmp_path)
    asyncio.run(runner.setup("Alice", "alice@example.com"))
    assert session.entered


def test_setup_runs_git_config_and_pip_install_in_order(tmp_path):
    runner, session = _make_runner(tmp_path=tmp_path)
    asyncio.run(runner.setup("Alice", "alice@example.com"))
    assert any(
        "git config --global user.name" in c and "Alice" in c
        for c in session.exec_calls
    )
    assert any(
        "git config --global user.email" in c and "alice@example.com" in c
        for c in session.exec_calls
    )
    assert any("pip install" in c for c in session.exec_calls)
    name_idx = next(i for i, c in enumerate(session.exec_calls) if "user.name" in c)
    email_idx = next(i for i, c in enumerate(session.exec_calls) if "user.email" in c)
    pip_idx = next(i for i, c in enumerate(session.exec_calls) if "pip install" in c)
    assert name_idx < email_idx < pip_idx


def test_setup_propagates_docker_error_when_pip_install_fails(tmp_path):
    session = FakeDockerSession(
        exec_handlers={"pip install": DockerError("pip install failed: exit 1")}
    )
    runner, _ = _make_runner(session=session, tmp_path=tmp_path)
    with pytest.raises(DockerError, match="pip install failed"):
        asyncio.run(runner.setup("Alice", "alice@example.com"))


# ── preflight() ──────────────────────────────────────────────────────────────


def test_preflight_returns_empty_list_on_clean_pass(tmp_path):
    runner, _ = _make_runner(tmp_path=tmp_path)
    result = asyncio.run(
        runner.preflight([("ruff", "ruff check ."), ("mypy", "mypy .")])
    )
    assert result == []


def test_preflight_returns_failure_tuples_for_failing_checks(tmp_path):
    session = FakeDockerSession(
        exec_handlers={"ruff check": DockerError("E501 line too long")}
    )
    runner, _ = _make_runner(session=session, tmp_path=tmp_path)
    result = asyncio.run(
        runner.preflight([("ruff", "ruff check ."), ("mypy", "mypy .")])
    )
    assert len(result) == 1
    name, cmd, output = result[0]
    assert name == "ruff"
    assert cmd == "ruff check ."
    assert "E501" in output


def test_preflight_runs_all_checks_when_one_fails(tmp_path):
    session = FakeDockerSession(
        exec_handlers={"ruff check": DockerError("ruff failed")}
    )
    runner, _ = _make_runner(session=session, tmp_path=tmp_path)
    asyncio.run(
        runner.preflight(
            [("ruff", "ruff check ."), ("mypy", "mypy ."), ("pytest", "pytest")]
        )
    )
    assert any("ruff check" in c for c in session.exec_calls)
    assert any("mypy" in c for c in session.exec_calls)
    assert any("pytest" in c for c in session.exec_calls)


def test_preflight_with_empty_checks_returns_empty(tmp_path):
    runner, _ = _make_runner(tmp_path=tmp_path)
    assert asyncio.run(runner.preflight([])) == []


# ── work() ───────────────────────────────────────────────────────────────────


def test_work_writes_prompt_to_container(tmp_path):
    runner, session = _make_runner(tmp_path=tmp_path)
    asyncio.run(runner.work(_ROLE, "Hello"))
    assert ("/tmp/.pycastle_prompt", "Hello") in session.write_calls


def test_work_returns_agent_output(tmp_path):
    runner, _ = _make_runner(tmp_path=tmp_path)
    result = asyncio.run(runner.work(_ROLE, "some prompt"))
    assert isinstance(result, CommitMessageOutput)


def test_work_calls_session_exec_stream_with_claude_command(tmp_path):
    runner, session = _make_runner(tmp_path=tmp_path, model="claude-sonnet-4-6")
    asyncio.run(runner.work(_ROLE, "prompt"))
    assert any("claude" in c for c in session.stream_calls)
    assert any("--model claude-sonnet-4-6" in c for c in session.stream_calls)


def test_work_called_twice_writes_each_prompt(tmp_path):
    """Calling work() twice with different prompts must write each prompt to the container."""
    chunk_lists = [[_COMPLETE_LINE], [_COMPLETE_LINE]]
    call_count = {"n": 0}
    session = FakeDockerSession()

    def _stream(command: str):
        session.stream_calls.append(command)
        return iter(chunk_lists[call_count["n"]])

    session.exec_stream = _stream  # type: ignore[method-assign]
    runner, _ = _make_runner(session=session, tmp_path=tmp_path)

    asyncio.run(runner.work(_ROLE, "First prompt"))
    call_count["n"] = 1
    asyncio.run(runner.work(_ROLE, "Second prompt"))

    prompt_writes = [c for c in session.write_calls if c[0] == "/tmp/.pycastle_prompt"]
    assert prompt_writes[0][1] == "First prompt"
    assert prompt_writes[1][1] == "Second prompt"


def test_work_updates_phase_to_work(tmp_path):
    display = RecordingStatusDisplay()
    runner, _ = _make_runner(name="impl-1", status_display=display, tmp_path=tmp_path)
    asyncio.run(runner.work(_ROLE, "prompt"))
    assert ("update_phase", "impl-1", "Work") in display.calls


def test_work_calls_reset_idle_timer(tmp_path):
    display = RecordingStatusDisplay()
    runner, _ = _make_runner(name="impl-1", status_display=display, tmp_path=tmp_path)
    asyncio.run(runner.work(_ROLE, "prompt"))
    assert ("reset_idle_timer", "impl-1") in display.calls


def test_work_raises_usage_limit_error_on_session_limit_in_stream(tmp_path):
    line = (
        b'{"type":"result","is_error":true,"api_error_status":429,'
        b'"result":"rate limited"}\n'
    )
    session = FakeDockerSession(stream_chunks=[line])
    runner, _ = _make_runner(session=session, tmp_path=tmp_path)
    with pytest.raises(UsageLimitError):
        asyncio.run(runner.work(_ROLE, "prompt"))


def test_work_uses_custom_logs_dir_from_cfg(tmp_path):
    custom_logs = tmp_path / "my_logs"
    runner, _ = _make_runner(name="my-task", cfg=Config(logs_dir=custom_logs))
    assert runner.log_path.parent == custom_logs


def _result_line(content: str) -> bytes:
    return (
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": content,
                "is_error": False,
            }
        ).encode()
        + b"\n"
    )


def _assistant_line(text: str) -> bytes:
    return (
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": text}]},
            }
        ).encode()
        + b"\n"
    )


def _assistant_with_usage_line(text: str, input_tokens: int) -> bytes:
    return (
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": text}],
                    "usage": {
                        "input_tokens": input_tokens,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            }
        ).encode()
        + b"\n"
    )


def _usage_limit_line() -> bytes:
    return (
        json.dumps(
            {
                "type": "result",
                "is_error": True,
                "api_error_status": 429,
                "result": "rate limit",
            }
        ).encode()
        + b"\n"
    )


def test_work_idle_timeout_raises_agent_timeout_error(tmp_path):
    event = threading.Event()
    session = FakeDockerSession()
    session.exec_stream = lambda cmd: (event.wait() or b"never" for _ in range(1))  # type: ignore[method-assign]

    cfg = Config(logs_dir=tmp_path, idle_timeout=0.05)
    runner, _ = _make_runner(session=session, cfg=cfg)
    with pytest.raises(AgentTimeoutError):
        asyncio.run(runner.work(_ROLE, "prompt"))


def test_work_log_first_line_is_pycastle_input_record_on_success(tmp_path):
    session = FakeDockerSession(
        stream_chunks=[_result_line("<commit_message>done</commit_message>")]
    )
    runner, _ = _make_runner(session=session, tmp_path=tmp_path)
    asyncio.run(runner.work(_ROLE, "test prompt"))
    first_line = runner.log_path.read_bytes().split(b"\n")[0]
    record = json.loads(first_line)
    assert record["type"] == "pycastle_input"
    assert record["role"] == "implementer"
    assert record["run_kind"] == "fresh"
    assert record["prompt"] == "test prompt"


def test_work_log_first_line_is_pycastle_input_record_on_agent_timeout(tmp_path):
    event = threading.Event()
    session = FakeDockerSession()
    session.exec_stream = lambda cmd: (event.wait() or b"never" for _ in range(1))  # type: ignore[method-assign]

    cfg = Config(logs_dir=tmp_path, idle_timeout=0.05)
    runner, _ = _make_runner(session=session, cfg=cfg)
    with pytest.raises(AgentTimeoutError):
        asyncio.run(runner.work(_ROLE, "stalled prompt"))
    first_line = runner.log_path.read_bytes().split(b"\n")[0]
    record = json.loads(first_line)
    assert record["type"] == "pycastle_input"
    assert record["prompt"] == "stalled prompt"


def test_work_log_first_line_is_pycastle_input_record_on_usage_limit_error(tmp_path):
    session = FakeDockerSession(stream_chunks=[_usage_limit_line()])
    runner, _ = _make_runner(session=session, tmp_path=tmp_path)
    with pytest.raises(UsageLimitError):
        asyncio.run(runner.work(_ROLE, "rate limited prompt"))
    first_line = runner.log_path.read_bytes().split(b"\n")[0]
    record = json.loads(first_line)
    assert record["type"] == "pycastle_input"
    assert record["prompt"] == "rate limited prompt"


def test_work_log_contains_all_chunk_bytes_after_header(tmp_path):
    chunk1 = b'{"type":"result","result":"<commit_message>done</commit_message>","is_error":false}'
    chunk2 = b"\n"
    session = FakeDockerSession(stream_chunks=[chunk1, chunk2])
    runner, _ = _make_runner(session=session, tmp_path=tmp_path)
    asyncio.run(runner.work(_ROLE, "prompt"))
    log_bytes = runner.log_path.read_bytes()
    _header, rest = log_bytes.split(b"\n", 1)
    assert rest == chunk1 + chunk2


def test_work_lines_split_across_chunk_boundaries_are_assembled(tmp_path):
    full_line = b'{"type":"result","result":"<commit_message>done</commit_message>","is_error":false}\n'
    mid = len(full_line) // 2
    session = FakeDockerSession(stream_chunks=[full_line[:mid], full_line[mid:]])
    runner, _ = _make_runner(session=session, tmp_path=tmp_path)
    result = asyncio.run(runner.work(_ROLE, "prompt"))
    assert isinstance(result, CommitMessageOutput)


def test_work_partial_final_line_without_newline_is_processed(tmp_path):
    line_bytes = b'{"type":"result","result":"<commit_message>done</commit_message>","is_error":false}'
    session = FakeDockerSession(stream_chunks=[line_bytes])
    runner, _ = _make_runner(session=session, tmp_path=tmp_path)
    result = asyncio.run(runner.work(_ROLE, "prompt"))
    assert isinstance(result, CommitMessageOutput)


def test_work_chunk_with_multiple_newlines_yields_all_lines(tmp_path):
    display = RecordingStatusDisplay()
    line1 = (
        b'{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}\n'
    )
    line2 = b'{"type":"result","result":"<commit_message>done</commit_message>","is_error":false}\n'
    session = FakeDockerSession(stream_chunks=[line1 + line2])
    runner, _ = _make_runner(session=session, status_display=display, tmp_path=tmp_path)
    result = asyncio.run(runner.work(_ROLE, "prompt"))
    assert isinstance(result, CommitMessageOutput)
    assert any(
        call[0] == "print" and "hello" in call[2]
        for call in display.calls
        if len(call) > 2
    )


def test_work_on_tokens_fires_when_usage_present(tmp_path):
    session = FakeDockerSession(
        stream_chunks=[
            _assistant_with_usage_line("thinking", 50_000),
            _result_line("<commit_message>done</commit_message>"),
        ]
    )
    display = RecordingStatusDisplay()
    runner, _ = _make_runner(
        name="impl-1", session=session, status_display=display, tmp_path=tmp_path
    )
    asyncio.run(runner.work(_ROLE, "prompt"))
    assert ("update_tokens", "impl-1", 50_000) in display.calls


def test_work_on_tokens_silent_when_no_usage(tmp_path):
    session = FakeDockerSession(
        stream_chunks=[
            _assistant_line("no usage"),
            _result_line("<commit_message>done</commit_message>"),
        ]
    )
    display = RecordingStatusDisplay()
    runner, _ = _make_runner(
        name="impl-1", session=session, status_display=display, tmp_path=tmp_path
    )
    asyncio.run(runner.work(_ROLE, "prompt"))
    assert not any(call[0] == "update_tokens" for call in display.calls)
