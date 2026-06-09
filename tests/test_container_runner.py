"""Tests for ContainerRunner using a fake DockerSession."""

import asyncio
import json
import threading
from pathlib import Path
from typing import cast

import pytest

from pycastle.agents.output_protocol import AgentRole, CommitMessageOutput
from pycastle.config import Config, load_config
from pycastle.session import RunKind
from pycastle.services.agent_service import AssistantTurn, Result
from pycastle.services.claude_service import ClaudeService
from pycastle.errors import AgentTimeoutError, DockerError, UsageLimitError
from pycastle.infrastructure.container_runner import ContainerRunner
from pycastle.infrastructure.docker_session import DockerSession
from pycastle.infrastructure.preflight_failure_interpreter import (
    PreflightCommandFailure,
)
from tests.support import RecordingStatusDisplay

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


class FakeService:
    name = "fake"

    def __init__(self, *, command: str = "fake run", events=None) -> None:
        self.command = command
        self.events = list(events or [Result("<commit_message>done</commit_message>")])
        self.build_command_calls: list[dict[str, object]] = []
        self.run_lines: list[str] = []

    def build_command(
        self,
        role=AgentRole.IMPLEMENTER,
        model="",
        effort="",
        run_kind=RunKind.FRESH,
        session_uuid=None,
        tool_policy=None,
    ) -> str:
        self.build_command_calls.append(
            {
                "role": role,
                "model": model,
                "effort": effort,
                "run_kind": run_kind,
                "session_uuid": session_uuid,
                "tool_policy": tool_policy,
            }
        )
        return self.command

    def run(self, lines, on_provider_session_id=None):
        self.run_lines = list(lines)
        if on_provider_session_id is not None:
            on_provider_session_id("provider-session-123")
        yield from self.events


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
        service=ClaudeService(),
    )
    return runner, session


# ── Constructor ──────────────────────────────────────────────────────────────


def test_container_runner_constructor_takes_session(tmp_path):
    session = FakeDockerSession()
    runner = ContainerRunner(
        "agent",
        cast(DockerSession, session),
        cfg=Config(logs_dir=tmp_path),
        service=ClaudeService(),
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


def test_preflight_returns_typed_failures_for_failing_checks(tmp_path):
    session = FakeDockerSession(
        exec_handlers={"ruff check": DockerError("E501 line too long")}
    )
    runner, _ = _make_runner(session=session, tmp_path=tmp_path)
    result = asyncio.run(
        runner.preflight([("ruff", "ruff check ."), ("mypy", "mypy .")])
    )
    assert len(result) == 1
    assert result[0].check_name == "ruff"
    assert result[0].command == "ruff check ."
    assert "E501" in result[0].output


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


def test_preflight_collects_raw_failures_without_classifying_missing_tools(tmp_path):
    session = FakeDockerSession(
        exec_handlers={
            "ruff check": DockerError("bash: ruff: command not found"),
            "mypy .": DockerError("src/app.py:1: error: boom"),
        }
    )
    runner, _ = _make_runner(session=session, tmp_path=tmp_path)

    result = asyncio.run(
        runner.preflight([("ruff", "ruff check ."), ("mypy", "mypy .")])
    )

    assert result == [
        PreflightCommandFailure(
            check_name="ruff",
            command="ruff check .",
            output="bash: ruff: command not found",
        ),
        PreflightCommandFailure(
            check_name="mypy",
            command="mypy .",
            output="src/app.py:1: error: boom",
        ),
    ]
    assert any("ruff check" in c for c in session.exec_calls)
    assert any("mypy" in c for c in session.exec_calls)


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


def test_work_logs_success_to_reserved_work_log(tmp_path):
    runner, _ = _make_runner(tmp_path=tmp_path)

    asyncio.run(runner.work(_ROLE, "some prompt"))

    log_lines = runner.log_path.read_text(encoding="utf-8").splitlines()
    assert json.loads(log_lines[0])["prompt"] == "some prompt"
    assert log_lines[1] == _COMPLETE_LINE.decode("utf-8").rstrip("\n")


def test_work_executes_selected_service_command(tmp_path):
    session = FakeDockerSession(stream_chunks=[b'{"type":"ignored"}\n'])
    service = FakeService(command="fake run --model demo")
    runner = ContainerRunner(
        "agent",
        cast(DockerSession, session),
        model="demo-model",
        effort="high",
        cfg=Config(logs_dir=tmp_path),
        service=cast(ClaudeService, service),
    )

    asyncio.run(
        runner.work(_ROLE, "prompt", run_kind=RunKind.RESUME, session_uuid="s1")
    )

    assert session.stream_calls == ["fake run --model demo"]
    assert service.build_command_calls == [
        {
            "role": _ROLE,
            "model": "demo-model",
            "effort": "high",
            "run_kind": RunKind.RESUME,
            "session_uuid": "s1",
            "tool_policy": None,
        }
    ]


def test_work_forwards_provider_session_callback_to_service_run(tmp_path):
    captured: list[str] = []
    session = FakeDockerSession(stream_chunks=[b'{"type":"ignored"}\n'])
    service = FakeService()
    runner = ContainerRunner(
        "agent",
        cast(DockerSession, session),
        cfg=Config(logs_dir=tmp_path),
        service=cast(ClaudeService, service),
    )

    result = asyncio.run(
        runner.work(_ROLE, "prompt", on_provider_session_id=captured.append)
    )

    assert isinstance(result, CommitMessageOutput)
    assert captured == ["provider-session-123"]
    assert service.run_lines == ['{"type":"ignored"}']


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


def test_work_reuses_reserved_log_path_across_invocations(tmp_path):
    runner, _ = _make_runner(tmp_path=tmp_path)

    first_path = runner.log_path
    asyncio.run(runner.work(_ROLE, "First prompt"))
    asyncio.run(runner.work(_ROLE, "Second prompt"))

    assert runner.log_path == first_path
    assert runner.log_path.exists()
    log_text = runner.log_path.read_text(encoding="utf-8")
    assert '"prompt": "First prompt"' in log_text
    assert '"prompt": "Second prompt"' in log_text


def test_work_text_reuses_runner_logical_session_after_work(tmp_path):
    session = FakeDockerSession(stream_chunks=[b'{"type":"ignored"}\n'])
    service = FakeService(events=[Result("plain text result")])
    runner = ContainerRunner(
        "agent",
        cast(DockerSession, session),
        cfg=Config(logs_dir=tmp_path),
        service=cast(ClaudeService, service),
    )

    first_path = runner.log_path
    asyncio.run(runner.work(_ROLE, "First prompt"))
    result = asyncio.run(runner.work_text("Second prompt"))

    assert result == "plain text result"
    assert runner.log_path == first_path
    log_text = runner.log_path.read_text(encoding="utf-8")
    assert '"prompt": "First prompt"' in log_text
    assert '"prompt": "Second prompt"' in log_text


def test_work_text_preserves_tool_policy_behavior_for_runtime_contract(tmp_path):
    from pycastle_agent_runtime.contracts import ToolPolicy
    from pycastle.services.flag_profiles import AgentToolPolicyGroup

    session = FakeDockerSession(stream_chunks=[b'{"type":"ignored"}\n'])
    service = FakeService(events=[Result("plain text result")])
    runner = ContainerRunner(
        "agent",
        cast(DockerSession, session),
        cfg=Config(logs_dir=tmp_path),
        service=cast(ClaudeService, service),
    )

    result = asyncio.run(runner.work_text("prompt", tool_policy=ToolPolicy.PARTIAL))

    assert result == "plain text result"
    assert service.build_command_calls == [
        {
            "role": AgentRole.IMPLEMENTER,
            "model": "",
            "effort": "",
            "run_kind": RunKind.FRESH,
            "session_uuid": None,
            "tool_policy": AgentToolPolicyGroup.PARTIAL,
        }
    ]


def test_container_runners_keep_logical_sessions_in_separate_agent_logs(tmp_path):
    first_runner, _ = _make_runner(name="agent", tmp_path=tmp_path)
    second_runner, _ = _make_runner(name="agent", tmp_path=tmp_path)

    asyncio.run(first_runner.work(_ROLE, "First prompt"))
    asyncio.run(second_runner.work(_ROLE, "Second prompt"))

    assert first_runner.log_path != second_runner.log_path
    assert "First prompt" in first_runner.log_path.read_text(encoding="utf-8")
    assert "Second prompt" not in first_runner.log_path.read_text(encoding="utf-8")
    assert "Second prompt" in second_runner.log_path.read_text(encoding="utf-8")
    assert "First prompt" not in second_runner.log_path.read_text(encoding="utf-8")


def test_work_logs_partial_output_before_agent_timeout(tmp_path):
    session = FakeDockerSession()
    service = FakeService()

    def _stalled_stream(_command: str):
        yield b'{"type":"assistant","message":"still working"}\n'
        threading.Event().wait(0.08)
        yield b'{"type":"result","result":"too late"}\n'

    session.exec_stream = _stalled_stream  # type: ignore[method-assign]
    runner = ContainerRunner(
        "agent",
        cast(DockerSession, session),
        cfg=Config(logs_dir=tmp_path, idle_timeout=0.05),
        service=cast(ClaudeService, service),
    )

    with pytest.raises(AgentTimeoutError, match="Agent idle for more than 0.05s"):
        asyncio.run(runner.work(_ROLE, "slow prompt"))

    log_text = runner.log_path.read_text(encoding="utf-8")
    assert '"prompt": "slow prompt"' in log_text
    assert '{"type":"assistant","message":"still working"}' in log_text


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


def test_work_cleans_up_prompt_file_when_stream_processing_fails(tmp_path):
    line = (
        b'{"type":"result","is_error":true,"api_error_status":429,'
        b'"result":"rate limited"}\n'
    )
    session = FakeDockerSession(stream_chunks=[line])
    runner, _ = _make_runner(session=session, tmp_path=tmp_path)
    with pytest.raises(UsageLimitError):
        asyncio.run(runner.work(_ROLE, "prompt"))
    assert session.exec_calls[-1] == "rm -f /tmp/.pycastle_prompt"
    log_text = runner.log_path.read_text(encoding="utf-8")
    assert '"prompt": "prompt"' in log_text
    assert line.decode("utf-8").rstrip("\n") in log_text


def test_work_cleans_up_prompt_file_after_success(tmp_path):
    runner, session = _make_runner(tmp_path=tmp_path)

    result = asyncio.run(runner.work(_ROLE, "prompt"))

    assert isinstance(result, CommitMessageOutput)
    assert session.exec_calls[-1] == "rm -f /tmp/.pycastle_prompt"


def test_work_uses_custom_logs_dir_from_cfg(tmp_path):
    custom_logs = tmp_path / "my_logs"
    runner, _ = _make_runner(name="my-task", cfg=Config(logs_dir=custom_logs))
    assert runner.log_path.parent == custom_logs


def test_container_runner_global_logs_dir_uses_project_root_effective_path(tmp_path):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / "config.py").write_text(
        "from pathlib import Path\nlogs_dir = Path('shared-logs')\n"
    )
    project_dir = tmp_path / "My Project"
    project_dir.mkdir()

    cfg = load_config(repo_root=project_dir, global_dir=global_dir)
    runner, _ = _make_runner(name="my-task", cfg=cfg)

    expected_dir = project_dir / "shared-logs" / "my-project"
    assert runner.log_path.parent.resolve() == expected_dir.resolve()
    assert runner.log_path.exists()


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


def test_work_hands_provider_lines_to_service_and_protocol(tmp_path):
    display = RecordingStatusDisplay()
    session = FakeDockerSession(
        stream_chunks=[b'{"type":"assistant","message":"hello"}\n']
    )
    service = FakeService(
        events=[
            AssistantTurn("hello from service"),
            Result("<commit_message>done</commit_message>"),
        ]
    )
    runner = ContainerRunner(
        "impl-1",
        cast(DockerSession, session),
        status_display=display,
        cfg=Config(logs_dir=tmp_path),
        service=cast(ClaudeService, service),
    )

    result = asyncio.run(runner.work(_ROLE, "prompt"))

    assert isinstance(result, CommitMessageOutput)
    assert result.message == "done"
    assert service.run_lines == ['{"type":"assistant","message":"hello"}']
    assert ("print", "impl-1", "hello from service", None) in display.calls


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
