"""Tests for ContainerRunner using a fake DockerSession."""

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from pycastle.agent_output_protocol import AgentRole, CompletionOutput
from pycastle.config import Config
from pycastle.container_runner import ContainerRunner, _build_claude_command
from pycastle.docker_session import DockerSession
from pycastle.errors import DockerError, UsageLimitError
from pycastle.iteration._deps import RecordingStatusDisplay

_ROLE = AgentRole.IMPLEMENTER

_COMPLETE_LINE = (
    b'{"type":"result","result":"<promise>COMPLETE</promise>","is_error":false}\n'
)


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

    def exec_stream(self, command: str) -> Iterator[bytes]:
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


def test_setup_registers_status_display_with_runner_name(tmp_path):
    display = RecordingStatusDisplay()
    runner, _ = _make_runner(name="impl-1", status_display=display, tmp_path=tmp_path)
    asyncio.run(runner.setup("Alice", "alice@example.com"))
    register_calls = [c for c in display.calls if c[0] == "register"]
    assert len(register_calls) == 1
    assert register_calls[0][1] == "impl-1"


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


def test_work_renders_and_writes_prompt_to_container(tmp_path):
    runner, session = _make_runner(tmp_path=tmp_path)
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("Hello {{NAME}}")

    asyncio.run(runner.work(_ROLE, prompt_file, {"NAME": "World"}))

    assert ("/tmp/.pycastle_prompt", "Hello World") in session.write_calls


def test_work_returns_agent_output(tmp_path):
    runner, _ = _make_runner(tmp_path=tmp_path)
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("hi")
    result = asyncio.run(runner.work(_ROLE, prompt_file, {}))
    assert isinstance(result, CompletionOutput)


def test_work_calls_session_exec_stream_with_claude_command(tmp_path):
    runner, session = _make_runner(tmp_path=tmp_path, model="claude-sonnet-4-6")
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("hi")
    asyncio.run(runner.work(_ROLE, prompt_file, {}))
    assert any("claude" in c for c in session.stream_calls)
    assert any("--model claude-sonnet-4-6" in c for c in session.stream_calls)


def test_work_called_twice_renders_each_calls_prompt_args(tmp_path):
    """Calling work() twice with different args must inject the new prompt each time."""
    session = FakeDockerSession(stream_chunks=[_COMPLETE_LINE, _COMPLETE_LINE])
    # exec_stream is consumed each call; rebuild iterator per call
    chunk_lists = [[_COMPLETE_LINE], [_COMPLETE_LINE]]

    def _stream(command: str) -> Iterator[bytes]:
        session.stream_calls.append(command)
        return iter(chunk_lists.pop(0))

    session.exec_stream = _stream  # type: ignore[method-assign]
    runner, _ = _make_runner(session=session, tmp_path=tmp_path)
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("Hello {{NAME}}")

    asyncio.run(runner.work(_ROLE, prompt_file, {"NAME": "First"}))
    asyncio.run(runner.work(_ROLE, prompt_file, {"NAME": "Second"}))

    prompt_writes = [c for c in session.write_calls if c[0] == "/tmp/.pycastle_prompt"]
    assert prompt_writes[0][1] == "Hello First"
    assert prompt_writes[1][1] == "Hello Second"


def test_work_expands_shell_expressions_via_session_exec(tmp_path):
    session = FakeDockerSession(exec_handlers={"echo hi": "expanded\n"})
    runner, _ = _make_runner(session=session, tmp_path=tmp_path)
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("Result: !`echo hi`")

    asyncio.run(runner.work(_ROLE, prompt_file, {}))

    prompt_writes = [c for c in session.write_calls if c[0] == "/tmp/.pycastle_prompt"]
    assert prompt_writes[0][1] == "Result: expanded"


def test_work_updates_phase_to_work(tmp_path):
    display = RecordingStatusDisplay()
    runner, _ = _make_runner(name="impl-1", status_display=display, tmp_path=tmp_path)
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("hi")
    asyncio.run(runner.work(_ROLE, prompt_file, {}))
    assert ("update_phase", "impl-1", "Work") in display.calls


def test_work_calls_reset_idle_timer(tmp_path):
    display = RecordingStatusDisplay()
    runner, _ = _make_runner(name="impl-1", status_display=display, tmp_path=tmp_path)
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("hi")
    asyncio.run(runner.work(_ROLE, prompt_file, {}))
    assert ("reset_idle_timer", "impl-1") in display.calls


def test_work_raises_usage_limit_error_on_session_limit_in_stream(tmp_path):
    session = FakeDockerSession(stream_chunks=[b"You've hit your session limit\n"])
    runner, _ = _make_runner(session=session, tmp_path=tmp_path)
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("hi")
    with pytest.raises(UsageLimitError):
        asyncio.run(runner.work(_ROLE, prompt_file, {}))


def test_work_uses_custom_logs_dir_from_cfg(tmp_path):
    custom_logs = tmp_path / "my_logs"
    runner, _ = _make_runner(name="my-task", cfg=Config(logs_dir=custom_logs))
    assert runner.log_path.parent == custom_logs
