import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from agent_runtime.runtime import (
    Completed,
    Continuation,
    RunResult,
    RuntimeOutcome,
    TimedOut,
)
from agent_runtime.contracts import ToolAccess, ToolPolicy
from agent_runtime.types import ResolvedProvider

from pycastle.agents.output_protocol import AgentRole, CommitMessageOutput
from pycastle.agents.runner import AgentRunner, RunRequest
from pycastle.config import Config
from pycastle.errors import AgentTimeoutError, UsageLimitError
from pycastle.prompts.dispatch import PromptInvocation
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.runtime_session import ProviderSessionState
from pycastle.services import GitService

from tests.support import RecordingStatusDisplay


class _FakeService:
    name = "codex"

    def build_env(
        self,
        state_dir_container_path: str | None = None,
        token: str | None = None,
    ) -> dict[str, str]:
        del state_dir_container_path, token
        return {}

    def is_available(self, now=None) -> bool:
        del now
        return True

    def next_wake_time(self):
        raise AssertionError("next_wake_time should not be called in this test")

    def mark_exhausted(self, reset_time, *, _now=None) -> None:
        del reset_time, _now

    def state_dir_relpath(self, role, namespace: str = "") -> str | None:
        del role, namespace
        return None

    def is_resumable(self, state_dir: Path) -> bool:
        del state_dir
        return False

    def valid_models(self) -> frozenset[str]:
        return frozenset({"gpt-5.5"})

    def valid_efforts(self) -> frozenset[str]:
        return frozenset({"medium"})

    def provider_session_preferences(self, request):
        del request
        raise AssertionError(
            "provider_session_preferences should not be called in this test"
        )

    def provider_session_state(self, request) -> ProviderSessionState:
        del request
        return ProviderSessionState(
            run_kind=None,  # type: ignore[arg-type]
            provider_session_id=None,
            auth_seed_action=None,
        )


class _RecordingService(_FakeService):
    def __init__(self, name: str) -> None:
        self.name = name
        self.mark_exhausted_calls: list[object] = []

    def mark_exhausted(self, reset_time, *, _now=None) -> None:
        del _now
        self.mark_exhausted_calls.append(reset_time)


class _FakeDockerSession:
    def __init__(self) -> None:
        self._container = type("Container", (), {"id": "container-123"})()
        self.exec_calls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def exec_simple(self, command: str, timeout: float | None = None) -> str:
        del timeout
        self.exec_calls.append(command)
        return ""


class _FakeRuntimeClient:
    def __init__(self, event: object) -> None:
        self._event = event

    async def run_new_session(self, request):
        request.on_live_output(self._event)
        return RuntimeOutcome(
            kind=Completed(),
            result=RunResult(
                output="<commit_message>done</commit_message>",
                usage=None,
                continuation=None,
                selected=ResolvedProvider(
                    service="codex",
                    model="gpt-5.5",
                    effort="medium",
                ),
            ),
        )


class _FakeRuntimeClientWithEvents:
    def __init__(self, events: list[object], *, output: str) -> None:
        self._events = events
        self._output = output

    async def run_new_session(self, request):
        for event in self._events:
            request.on_live_output(event)
        return RuntimeOutcome(
            kind=Completed(),
            result=RunResult(
                output=self._output,
                usage=None,
                continuation=None,
                selected=ResolvedProvider(
                    service="codex",
                    model="gpt-5.5",
                    effort="medium",
                ),
            ),
        )


class _AssertingRuntimeClient:
    def __init__(self, status_display: RecordingStatusDisplay, agent_name: str) -> None:
        self._status_display = status_display
        self._agent_name = agent_name

    async def run_new_session(self, request):
        assert (self._agent_name, "Work") in self._status_display.phase_updates
        return RuntimeOutcome(
            kind=Completed(),
            result=RunResult(
                output="<commit_message>done</commit_message>",
                usage=None,
                continuation=None,
                selected=ResolvedProvider(
                    service="codex",
                    model="gpt-5.5",
                    effort="medium",
                ),
            ),
        )


class _BlockingRuntimeClient:
    def __init__(
        self,
        status_display: RecordingStatusDisplay,
        agent_name: str,
        started: asyncio.Event,
        finish: asyncio.Event,
    ) -> None:
        self._status_display = status_display
        self._agent_name = agent_name
        self._started = started
        self._finish = finish

    async def run_new_session(self, request):
        del request
        assert (self._agent_name, "Work") in self._status_display.phase_updates
        self._started.set()
        await self._finish.wait()
        return RuntimeOutcome(
            kind=Completed(),
            result=RunResult(
                output="<commit_message>done</commit_message>",
                usage=None,
                continuation=None,
                selected=ResolvedProvider(
                    service="codex",
                    model="gpt-5.5",
                    effort="medium",
                ),
            ),
        )


class _TimedOutRuntimeClient:
    def __init__(self, continuation: Continuation) -> None:
        self.continuation = continuation
        self.new_session_calls = 0
        self.resumed_session_calls = 0

    async def run_new_session(self, request):
        del request
        self.new_session_calls += 1
        return RuntimeOutcome(
            kind=TimedOut(),
            result=RunResult(
                output="",
                usage=None,
                continuation=self.continuation,
                selected=ResolvedProvider(
                    service="opencode",
                    model="open-code",
                    effort="medium",
                ),
            ),
        )

    async def run_resumed_session(self, request):
        del request
        self.resumed_session_calls += 1
        raise AssertionError("OpenCode timeout should not enter the resume loop")


class _RetryingTimedOutRuntimeClient:
    def __init__(self, continuation: Continuation) -> None:
        self.continuation = continuation
        self.new_session_calls = 0
        self.resumed_session_calls = 0

    async def run_new_session(self, request):
        del request
        self.new_session_calls += 1
        return RuntimeOutcome(
            kind=TimedOut(),
            result=RunResult(
                output="",
                usage=None,
                continuation=self.continuation,
                selected=ResolvedProvider(
                    service="codex",
                    model="gpt-5.5",
                    effort="medium",
                ),
            ),
        )

    async def run_resumed_session(self, request):
        del request
        self.resumed_session_calls += 1
        return RuntimeOutcome(
            kind=TimedOut(),
            result=RunResult(
                output="",
                usage=None,
                continuation=self.continuation,
                selected=ResolvedProvider(
                    service="codex",
                    model="gpt-5.5",
                    effort="medium",
                ),
            ),
        )


def _run_agent_with_live_event(tmp_path, monkeypatch, event: object):
    repo_root = tmp_path / "repo"
    mount_path = repo_root / "pycastle" / ".worktrees" / "issue-1898"
    mount_path.mkdir(parents=True)

    git_service = MagicMock(spec=GitService)
    git_service.get_user_name.return_value = "Test User"
    git_service.get_user_email.return_value = "test@example.com"
    runner = AgentRunner(
        env={},
        cfg=Config(logs_dir=tmp_path / "logs"),
        git_service=git_service,
        service_registry={"codex": _FakeService()},
    )
    runtime_client = _FakeRuntimeClient(event)
    status_display = RecordingStatusDisplay()

    monkeypatch.setattr(
        runner, "_build_session", lambda *_args, **_kwargs: _FakeDockerSession()
    )
    monkeypatch.setattr(
        runner,
        "_render_runtime_prompt",
        AsyncMock(return_value="prompt"),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner.setup",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner._get_runtime_client",
        lambda _self: runtime_client,
    )

    result = asyncio.run(
        runner.run(
            RunRequest(
                name="Implement Agent #1898",
                prompt=PromptInvocation(
                    template=PromptTemplate.IMPLEMENT_BEHAVIOR,
                    scope_args={
                        "ISSUE_NUMBER": "1898",
                        "ISSUE_TITLE": "Fix Codex terminal live output in AgentRunner",
                        "ISSUE_BODY": "",
                        "ISSUE_COMMENTS": "",
                        "BRANCH": "issue-1898",
                        "INTERRUPTED_WORK": "",
                    },
                ),
                mount_path=mount_path,
                role=AgentRole.IMPLEMENTER,
                model="gpt-5.5",
                effort="medium",
                service="codex",
                status_display=status_display,
            )
        )
    )
    return result, status_display


def test_agent_runner_captures_raw_provider_output_for_all_live_events_in_log(
    tmp_path,
    monkeypatch,
):
    repo_root = tmp_path / "repo"
    mount_path = repo_root / "pycastle" / ".worktrees" / "issue-1899"
    mount_path.mkdir(parents=True)
    logs_dir = tmp_path / "logs"

    git_service = MagicMock(spec=GitService)
    git_service.get_user_name.return_value = "Test User"
    git_service.get_user_email.return_value = "test@example.com"
    runner = AgentRunner(
        env={},
        cfg=Config(logs_dir=logs_dir),
        git_service=git_service,
        service_registry={"codex": _FakeService()},
    )
    runtime_client = _FakeRuntimeClientWithEvents(
        [
            SimpleNamespace(
                type="protocol",
                display_message="thread.started",
                raw_provider_output='{"type":"thread.started"}',
            ),
            SimpleNamespace(
                type="agent_message",
                display_message="live output text",
                raw_provider_output='{"type":"agent_message","text":"live output text"}',
            ),
            SimpleNamespace(
                type="protocol",
                display_message="turn.completed",
                raw_provider_output='{"type":"turn.completed"}',
            ),
        ],
        output="<commit_message>done</commit_message>",
    )
    status_display = RecordingStatusDisplay()

    monkeypatch.setattr(
        runner, "_build_session", lambda *_args, **_kwargs: _FakeDockerSession()
    )
    monkeypatch.setattr(
        runner,
        "_render_runtime_prompt",
        AsyncMock(return_value="prompt"),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner.setup",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner._get_runtime_client",
        lambda _self: runtime_client,
    )

    result = asyncio.run(
        runner.run(
            RunRequest(
                name="Implement Agent #1899",
                prompt=PromptInvocation(
                    template=PromptTemplate.IMPLEMENT_BEHAVIOR,
                    scope_args={
                        "ISSUE_NUMBER": "1899",
                        "ISSUE_TITLE": "Wire agent invocation log capture in AgentRunner",
                        "ISSUE_BODY": "",
                        "ISSUE_COMMENTS": "",
                        "BRANCH": "issue-1899",
                        "INTERRUPTED_WORK": "",
                    },
                ),
                mount_path=mount_path,
                role=AgentRole.IMPLEMENTER,
                model="gpt-5.5",
                effort="medium",
                service="codex",
                status_display=status_display,
            )
        )
    )

    assert isinstance(result, CommitMessageOutput)
    log_files = list(logs_dir.glob("*.log"))
    assert len(log_files) == 1
    log_text = log_files[0].read_text(encoding="utf-8")
    assert log_text
    assert '{"type":"thread.started"}\n' in log_text
    assert '{"type":"agent_message","text":"live output text"}\n' in log_text
    assert '{"type":"turn.completed"}\n' in log_text


def test_agent_runner_captures_final_response_when_live_output_has_no_raw_provider_log(
    tmp_path,
    monkeypatch,
):
    repo_root = tmp_path / "repo"
    mount_path = repo_root / "pycastle" / ".worktrees" / "issue-1899"
    mount_path.mkdir(parents=True)
    logs_dir = tmp_path / "logs"

    git_service = MagicMock(spec=GitService)
    git_service.get_user_name.return_value = "Test User"
    git_service.get_user_email.return_value = "test@example.com"
    runner = AgentRunner(
        env={},
        cfg=Config(logs_dir=logs_dir),
        git_service=git_service,
        service_registry={"codex": _FakeService()},
    )
    runtime_client = _FakeRuntimeClientWithEvents(
        [SimpleNamespace(type="protocol", display_message="thread.started")],
        output="<commit_message>done</commit_message>",
    )
    status_display = RecordingStatusDisplay()

    monkeypatch.setattr(
        runner, "_build_session", lambda *_args, **_kwargs: _FakeDockerSession()
    )
    monkeypatch.setattr(
        runner,
        "_render_runtime_prompt",
        AsyncMock(return_value="prompt"),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner.setup",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner._get_runtime_client",
        lambda _self: runtime_client,
    )

    result = asyncio.run(
        runner.run(
            RunRequest(
                name="Implement Agent #1899",
                prompt=PromptInvocation(
                    template=PromptTemplate.IMPLEMENT_BEHAVIOR,
                    scope_args={
                        "ISSUE_NUMBER": "1899",
                        "ISSUE_TITLE": "Wire agent invocation log capture in AgentRunner",
                        "ISSUE_BODY": "",
                        "ISSUE_COMMENTS": "",
                        "BRANCH": "issue-1899",
                        "INTERRUPTED_WORK": "",
                    },
                ),
                mount_path=mount_path,
                role=AgentRole.IMPLEMENTER,
                model="gpt-5.5",
                effort="medium",
                service="codex",
                status_display=status_display,
            )
        )
    )

    assert isinstance(result, CommitMessageOutput)
    log_files = list(logs_dir.glob("*.log"))
    assert len(log_files) == 1
    log_text = log_files[0].read_text(encoding="utf-8")
    assert "<commit_message>done</commit_message>\n" in log_text


def test_agent_runner_prints_live_agent_message_events_without_event_type(
    tmp_path,
    monkeypatch,
):
    result, status_display = _run_agent_with_live_event(
        tmp_path,
        monkeypatch,
        SimpleNamespace(type="agent_message", display_message="live output text"),
    )

    assert isinstance(result, CommitMessageOutput)
    assert (
        "print",
        "Implement Agent #1898",
        "live output text",
        None,
    ) in status_display.calls


def test_agent_runner_suppresses_non_agent_live_output_events(tmp_path, monkeypatch):
    result, status_display = _run_agent_with_live_event(
        tmp_path,
        monkeypatch,
        SimpleNamespace(type="other", display_message="thread.started"),
    )

    assert isinstance(result, CommitMessageOutput)
    assert ("reset_idle_timer", "Implement Agent #1898") in status_display.calls
    assert ("print", "Implement Agent #1898", "thread.started", None) not in (
        status_display.calls
    )


def test_agent_runner_skips_blank_live_agent_message_events(tmp_path, monkeypatch):
    result, status_display = _run_agent_with_live_event(
        tmp_path,
        monkeypatch,
        SimpleNamespace(type="agent_message", display_message=""),
    )

    assert isinstance(result, CommitMessageOutput)
    assert ("reset_idle_timer", "Implement Agent #1898") in status_display.calls
    assert not any(
        call[0] == "print" and call[1] == "Implement Agent #1898"
        for call in status_display.calls
    )


def test_agent_runner_switches_runtime_rows_to_work_before_runtime_invocation(
    tmp_path,
    monkeypatch,
):
    mount_path = tmp_path / "repo" / "pycastle" / ".worktrees" / "issue-1905"
    mount_path.mkdir(parents=True)

    git_service = MagicMock(spec=GitService)
    git_service.get_user_name.return_value = "Test User"
    git_service.get_user_email.return_value = "test@example.com"
    runner = AgentRunner(
        env={},
        cfg=Config(logs_dir=tmp_path / "logs"),
        git_service=git_service,
        service_registry={"codex": _FakeService()},
    )
    agent_name = "Implement Agent #1905"
    status_display = RecordingStatusDisplay()
    runtime_client = _AssertingRuntimeClient(status_display, agent_name)

    monkeypatch.setattr(
        runner, "_build_session", lambda *_args, **_kwargs: _FakeDockerSession()
    )
    monkeypatch.setattr(
        runner,
        "_render_runtime_prompt",
        AsyncMock(return_value="prompt"),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner.setup",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner._get_runtime_client",
        lambda _self: runtime_client,
    )

    result = asyncio.run(
        runner.run(
            RunRequest(
                name=agent_name,
                prompt=PromptInvocation(
                    template=PromptTemplate.IMPLEMENT_BEHAVIOR,
                    scope_args={
                        "ISSUE_NUMBER": "1905",
                        "ISSUE_TITLE": "Fix Setup to Work phase transition",
                        "ISSUE_BODY": "",
                        "ISSUE_COMMENTS": "",
                        "BRANCH": "issue-1905",
                        "INTERRUPTED_WORK": "",
                    },
                ),
                mount_path=mount_path,
                role=AgentRole.IMPLEMENTER,
                model="gpt-5.5",
                effort="medium",
                service="codex",
                status_display=status_display,
            )
        )
    )

    assert isinstance(result, CommitMessageOutput)
    assert (agent_name, "Work") in status_display.phase_updates


def test_agent_runner_parallel_runtime_rows_switch_to_work_independently(
    tmp_path,
    monkeypatch,
):
    repo_root = tmp_path / "repo" / "pycastle" / ".worktrees"
    mount_a = repo_root / "issue-1905-a"
    mount_b = repo_root / "issue-1905-b"
    mount_a.mkdir(parents=True)
    mount_b.mkdir(parents=True)

    git_service = MagicMock(spec=GitService)
    git_service.get_user_name.return_value = "Test User"
    git_service.get_user_email.return_value = "test@example.com"
    runner = AgentRunner(
        env={},
        cfg=Config(logs_dir=tmp_path / "logs"),
        git_service=git_service,
        service_registry={"codex": _FakeService()},
    )
    status_display = RecordingStatusDisplay()
    agent_a = "Implement Agent #1905-A"
    agent_b = "Implement Agent #1905-B"
    setup_a = asyncio.Event()
    setup_b = asyncio.Event()
    runtime_a_started = asyncio.Event()
    runtime_b_started = asyncio.Event()
    finish_a = asyncio.Event()
    finish_b = asyncio.Event()
    runtime_clients = {
        agent_a: _BlockingRuntimeClient(
            status_display, agent_a, runtime_a_started, finish_a
        ),
        agent_b: _BlockingRuntimeClient(
            status_display, agent_b, runtime_b_started, finish_b
        ),
    }

    monkeypatch.setattr(
        runner, "_build_session", lambda *_args, **_kwargs: _FakeDockerSession()
    )
    monkeypatch.setattr(
        runner,
        "_render_runtime_prompt",
        AsyncMock(return_value="prompt"),
    )

    async def setup_side_effect(self, git_name, git_email, work_body=""):
        del git_name, git_email, work_body
        if self.name == agent_a:
            await setup_a.wait()
            return None
        if self.name == agent_b:
            await setup_b.wait()
            return None
        raise AssertionError(f"unexpected setup call for {self.name}")

    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner.setup",
        setup_side_effect,
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner._get_runtime_client",
        lambda self: runtime_clients[self.name],
    )

    async def run_agents() -> tuple[CommitMessageOutput, CommitMessageOutput]:
        task_a = asyncio.create_task(
            runner.run(
                RunRequest(
                    name=agent_a,
                    prompt=PromptInvocation(
                        template=PromptTemplate.IMPLEMENT_BEHAVIOR,
                        scope_args={
                            "ISSUE_NUMBER": "1905",
                            "ISSUE_TITLE": "Fix Setup to Work phase transition",
                            "ISSUE_BODY": "",
                            "ISSUE_COMMENTS": "",
                            "BRANCH": "issue-1905-a",
                            "INTERRUPTED_WORK": "",
                        },
                    ),
                    mount_path=mount_a,
                    role=AgentRole.IMPLEMENTER,
                    model="gpt-5.5",
                    effort="medium",
                    service="codex",
                    status_display=status_display,
                )
            )
        )
        task_b = asyncio.create_task(
            runner.run(
                RunRequest(
                    name=agent_b,
                    prompt=PromptInvocation(
                        template=PromptTemplate.IMPLEMENT_BEHAVIOR,
                        scope_args={
                            "ISSUE_NUMBER": "1906",
                            "ISSUE_TITLE": "Keep parallel runtime rows independent",
                            "ISSUE_BODY": "",
                            "ISSUE_COMMENTS": "",
                            "BRANCH": "issue-1905-b",
                            "INTERRUPTED_WORK": "",
                        },
                    ),
                    mount_path=mount_b,
                    role=AgentRole.IMPLEMENTER,
                    model="gpt-5.5",
                    effort="medium",
                    service="codex",
                    status_display=status_display,
                )
            )
        )

        setup_a.set()
        await runtime_a_started.wait()
        assert (agent_a, "Work") in status_display.phase_updates
        assert (agent_b, "Work") not in status_display.phase_updates

        setup_b.set()
        await runtime_b_started.wait()
        finish_a.set()
        finish_b.set()
        return await asyncio.gather(task_a, task_b)

    result_a, result_b = asyncio.run(run_agents())

    assert isinstance(result_a, CommitMessageOutput)
    assert isinstance(result_b, CommitMessageOutput)
    assert (agent_a, "Work") in status_display.phase_updates
    assert (agent_b, "Work") in status_display.phase_updates


def test_agent_runner_preflight_keeps_container_preflight_phase_names(
    tmp_path,
    monkeypatch,
):
    mount_path = tmp_path / "repo" / "pycastle" / ".worktrees" / "issue-1905"
    mount_path.mkdir(parents=True)

    git_service = MagicMock(spec=GitService)
    git_service.get_user_name.return_value = "Test User"
    git_service.get_user_email.return_value = "test@example.com"
    runner = AgentRunner(
        env={},
        cfg=Config(
            logs_dir=tmp_path / "logs",
            preflight_checks=[
                ("Ruff", "ruff check"),
                ("Pytest", "pytest"),
            ],
        ),
        git_service=git_service,
        service_registry={"codex": _FakeService()},
    )
    status_display = RecordingStatusDisplay()

    monkeypatch.setattr(
        runner,
        "_build_preflight_session",
        lambda *_args, **_kwargs: _FakeDockerSession(),
    )

    failures = asyncio.run(
        runner.run_preflight(
            name="Preflight Agent #1905",
            mount_path=mount_path,
            stage="implement",
            status_display=status_display,
            work_body="Fix Setup to Work phase transition",
        )
    )

    assert failures == []
    assert ("Preflight Agent #1905", "Work") not in status_display.phase_updates
    assert status_display.phase_updates == [
        ("Preflight Agent #1905", "Running Ruff (1/2)"),
        ("Preflight Agent #1905", "Running Pytest (2/2)"),
    ]


def test_agent_runner_routes_opencode_timeout_to_usage_limit_without_retries(
    tmp_path,
    monkeypatch,
):
    mount_path = tmp_path / "repo" / "pycastle" / ".worktrees" / "issue-1920"
    mount_path.mkdir(parents=True)

    git_service = MagicMock(spec=GitService)
    git_service.get_user_name.return_value = "Test User"
    git_service.get_user_email.return_value = "test@example.com"
    service = _RecordingService("opencode")
    runner = AgentRunner(
        env={},
        cfg=Config(
            logs_dir=tmp_path / "logs",
            timeout_retries=3,
            opencode_minimum_unknown_reset_duration_hours=1.0,
        ),
        git_service=git_service,
        service_registry={"opencode": service},
    )
    status_display = RecordingStatusDisplay()
    continuation = Continuation(serialized="opaque-continuation")
    runtime_client = _TimedOutRuntimeClient(continuation)
    now = datetime(2026, 6, 27, 12, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(
        runner, "_build_session", lambda *_args, **_kwargs: _FakeDockerSession()
    )
    monkeypatch.setattr(
        runner,
        "_render_runtime_prompt",
        AsyncMock(return_value="prompt"),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner.setup",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner._get_runtime_client",
        lambda _self: runtime_client,
    )
    monkeypatch.setattr("pycastle.agents.runner._time_module.now_local", lambda: now)

    with pytest.raises(UsageLimitError) as excinfo:
        asyncio.run(
            runner.run(
                RunRequest(
                    name="Implement Agent #1920",
                    prompt=PromptInvocation(
                        template=PromptTemplate.IMPLEMENT_BEHAVIOR,
                        scope_args={
                            "ISSUE_NUMBER": "1920",
                            "ISSUE_TITLE": "Route OpenCode TimedOut to UsageLimitError",
                            "ISSUE_BODY": "",
                            "ISSUE_COMMENTS": "",
                            "BRANCH": "issue-1920",
                            "INTERRUPTED_WORK": "",
                        },
                    ),
                    mount_path=mount_path,
                    role=AgentRole.IMPLEMENTER,
                    model="open-code",
                    effort="medium",
                    service="opencode",
                    status_display=status_display,
                )
            )
        )

    assert excinfo.value.provider == "opencode"
    assert runtime_client.new_session_calls == 1
    assert runtime_client.resumed_session_calls == 0
    assert service.mark_exhausted_calls == [
        datetime(2026, 6, 27, 14, 0, tzinfo=timezone.utc)
    ]
    assert not any(
        call[0] == "print" and "Timeout — restarting" in str(call[2])
        for call in status_display.calls
    )
    assert (
        mount_path / ".pycastle-session" / "implementer" / "_continuation"
    ).read_text(encoding="utf-8") == "opaque-continuation"
    assert {
        "caller": "Implement Agent #1920",
        "shutdown_message": "usage limit reached",
        "shutdown_style": "interrupted",
    } in status_display.remove_calls


def test_agent_runner_keeps_retry_loop_for_non_opencode_timeouts(
    tmp_path,
    monkeypatch,
):
    mount_path = tmp_path / "repo" / "pycastle" / ".worktrees" / "issue-1920-codex"
    mount_path.mkdir(parents=True)

    git_service = MagicMock(spec=GitService)
    git_service.get_user_name.return_value = "Test User"
    git_service.get_user_email.return_value = "test@example.com"
    runner = AgentRunner(
        env={},
        cfg=Config(logs_dir=tmp_path / "logs", timeout_retries=1),
        git_service=git_service,
        service_registry={"codex": _RecordingService("codex")},
    )
    status_display = RecordingStatusDisplay()
    continuation = Continuation(
        selected_service="codex",
        selected_model="gpt-5.5",
        selected_effort="medium",
        tool_access=ToolAccess(
            kind="none",
            workspace=None,
            tool_policy=ToolPolicy.NONE,
        ),
        provider_resume_state={},
    )
    runtime_client = _RetryingTimedOutRuntimeClient(continuation)

    monkeypatch.setattr(
        runner, "_build_session", lambda *_args, **_kwargs: _FakeDockerSession()
    )
    monkeypatch.setattr(
        runner,
        "_render_runtime_prompt",
        AsyncMock(return_value="prompt"),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner.setup",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner._get_runtime_client",
        lambda _self: runtime_client,
    )

    with pytest.raises(AgentTimeoutError):
        asyncio.run(
            runner.run(
                RunRequest(
                    name="Implement Agent #1920 Codex",
                    prompt=PromptInvocation(
                        template=PromptTemplate.IMPLEMENT_BEHAVIOR,
                        scope_args={
                            "ISSUE_NUMBER": "1920",
                            "ISSUE_TITLE": "Keep retries for non-OpenCode timeouts",
                            "ISSUE_BODY": "",
                            "ISSUE_COMMENTS": "",
                            "BRANCH": "issue-1920-codex",
                            "INTERRUPTED_WORK": "",
                        },
                    ),
                    mount_path=mount_path,
                    role=AgentRole.IMPLEMENTER,
                    model="gpt-5.5",
                    effort="medium",
                    service="codex",
                    status_display=status_display,
                )
            )
        )

    assert runtime_client.new_session_calls == 1
    assert runtime_client.resumed_session_calls == 1
    assert (
        mount_path / ".pycastle-session" / "implementer" / "_continuation"
    ).read_text(encoding="utf-8") == continuation.serialized
    assert (
        "print",
        "Implement Agent #1920 Codex",
        "Timeout — restarting (attempt 1/1)",
        None,
    ) in status_display.calls
    assert {
        "caller": "Implement Agent #1920 Codex",
        "shutdown_message": "timed out",
        "shutdown_style": "interrupted",
    } in status_display.remove_calls
