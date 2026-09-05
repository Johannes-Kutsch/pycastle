import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from agent_runtime.contracts import ToolAccess, ToolPolicy
from agent_runtime.errors import (
    AgentCredentialFailureError,
    HardAgentError,
    ProviderUnavailableReason,
)
from agent_runtime.runtime import (
    Completed,
    Continuation,
    ModelNotAvailable,
    ProviderUnavailable,
    RunResult,
    RuntimeOutcome,
    TimedOut,
    UsageLimited,
)
from agent_runtime.types import ResolvedProvider

from pycastle.agents.output_protocol import (
    _HANDLERS,
    AgentRole,
    CommitMessageOutput,
    PlannerOutput,
)
from pycastle.agents.result import CancellationToken
from pycastle.agents.runner import AgentRunner, RunRequest
from pycastle.config import Config
from pycastle.errors import (
    AgentTimeoutError,
    ModelNotAvailableError,
    TransientAgentError,
    UsageLimitError,
)
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

    def is_available(self, now=None, *, model=None) -> bool:
        del now, model
        return True

    def next_wake_time(self):
        raise AssertionError("next_wake_time should not be called in this test")

    def mark_exhausted(self, reset_time, *, _now=None) -> None:
        del reset_time, _now

    def mark_model_restricted(self, model: str) -> None:
        del model

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

    def auth_seed_action(self, provider_state_dir) -> None:
        del provider_state_dir

    def summary_line(self) -> str | None:
        return None


class _RecordingService(_FakeService):
    def __init__(self, name: str) -> None:
        self.name = name
        self.mark_exhausted_calls: list[object] = []
        self.mark_model_restricted_calls: list[str] = []

    def mark_exhausted(self, reset_time, *, _now=None) -> None:
        del _now
        self.mark_exhausted_calls.append(reset_time)

    def mark_model_restricted(self, model: str) -> None:
        self.mark_model_restricted_calls.append(model)


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
            kind=UsageLimited(reset_time=None, is_permanent=False),
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
        raise AssertionError("OpenCode usage limit should not enter the resume loop")


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


class _PlannerProtocolRetryRuntimeClient:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def run_new_session(self, request):
        self.prompts.append(request.prompt)
        if len(self.prompts) == 1:
            return RuntimeOutcome(
                kind=Completed(),
                result=RunResult(
                    output='<plan>"{\\"issues\\": []}"</plan>',
                    usage=None,
                    continuation=None,
                    selected=ResolvedProvider(
                        service="codex",
                        model="gpt-5.5",
                        effort="medium",
                    ),
                ),
            )
        return RuntimeOutcome(
            kind=Completed(),
            result=RunResult(
                output='<plan>{"issues": [], "blocked": []}</plan>',
                usage=None,
                continuation=None,
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
        "pycastle.agents.runner.render_prompt_invocation",
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
                        "OPERATING_BRANCH": "main",
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


def test_handlers_covers_all_roles():
    assert len(_HANDLERS) == len(AgentRole)


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
        "pycastle.agents.runner.render_prompt_invocation",
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
                        "OPERATING_BRANCH": "main",
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
        "pycastle.agents.runner.render_prompt_invocation",
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
                        "OPERATING_BRANCH": "main",
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
        "pycastle.agents.runner.render_prompt_invocation",
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
                        "OPERATING_BRANCH": "main",
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
        "pycastle.agents.runner.render_prompt_invocation",
        AsyncMock(return_value="prompt"),
    )

    async def setup_side_effect(self, git_name, git_email, work_body=""):
        del git_name, git_email, work_body
        if self.name == agent_a:
            await setup_a.wait()
            return
        if self.name == agent_b:
            await setup_b.wait()
            return
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
                            "OPERATING_BRANCH": "main",
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
                            "OPERATING_BRANCH": "main",
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
    now = datetime(2026, 6, 27, 12, 30, tzinfo=UTC)

    monkeypatch.setattr(
        runner, "_build_session", lambda *_args, **_kwargs: _FakeDockerSession()
    )
    monkeypatch.setattr(
        "pycastle.agents.runner.render_prompt_invocation",
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
                            "OPERATING_BRANCH": "main",
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
    assert service.mark_exhausted_calls == [datetime(2026, 6, 27, 14, 0, tzinfo=UTC)]
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
        "pycastle.agents.runner.render_prompt_invocation",
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
                            "OPERATING_BRANCH": "main",
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


def test_agent_runner_retries_malformed_planner_output_with_planner_specific_protocol_correction(
    tmp_path,
    monkeypatch,
):
    mount_path = tmp_path / "repo" / "pycastle" / ".worktrees" / "plan"
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
    status_display = RecordingStatusDisplay()
    runtime_client = _PlannerProtocolRetryRuntimeClient()

    invocation = PromptInvocation(
        template=PromptTemplate.PLAN,
        scope_args={
            "ALL_OPEN_ISSUES_JSON": '[{"number": 1, "title": "Fix A"}]',
            "READY_FOR_AGENT_ISSUES_JSON": '[{"number": 1, "title": "Fix A"}]',
        },
    )

    monkeypatch.setattr(
        runner, "_build_session", lambda *_args, **_kwargs: _FakeDockerSession()
    )
    monkeypatch.setattr(
        "pycastle.agents.runner.render_prompt_invocation",
        AsyncMock(return_value="initial planner prompt"),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner.setup",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner._get_runtime_client",
        lambda _self: runtime_client,
    )
    monkeypatch.setattr(
        "pycastle.prompts.pipeline.PromptRenderer.render_expected_output_shape",
        lambda self, template, scope_args: (
            "<plan>{...}</plan>"
            if template is PromptTemplate.PLAN and scope_args is invocation.scope_args
            else ""
        ),
    )

    result = asyncio.run(
        runner.run(
            RunRequest(
                name="Plan Agent",
                prompt=invocation,
                mount_path=mount_path,
                role=AgentRole.PLANNER,
                model="gpt-5.5",
                effort="medium",
                service="codex",
                status_display=status_display,
            )
        )
    )

    assert result == PlannerOutput(issues=[], blocked=[])
    assert runtime_client.prompts == [
        "initial planner prompt",
        (
            "Your last response did not include the required protocol output.\n"
            "Please review the task requirements and try again, making sure to include the required output tag.\n"
            "The parser reported the following error:\n"
            "Plan JSON must be an object, got str.\n"
            'Output tail: \'<plan>"{\\\\"issues\\\\": []}"</plan>\'\n'
            "On retry, return a raw JSON object in a `<plan>` tag (do not quote or escape the JSON).\n"
            "Use this Planner output shape exactly:\n"
            "<plan>{...}</plan>"
        ),
    ]


_VALID_STALE_CONTINUATION = json.dumps(
    {
        "service_name": "codex",
        "model": "gpt-5.5",
        "effort": "medium",
        "tool_access": {
            "kind": "none",
            "workspace": None,
            "tool_policy": {"kind": "tool_policy", "value": "none"},
        },
        "provider_resume_state": {"session_id": "expired-codex-session"},
    }
)


class _StaleResumeRuntimeClient:
    """Raises ContinuationUnrecoverableError on resume; succeeds on new."""

    def __init__(self) -> None:
        self.run_new_session_calls = 0
        self.run_resumed_session_calls = 0

    async def run_resumed_session(self, request):
        del request
        from agent_runtime.errors import ContinuationUnrecoverableError

        self.run_resumed_session_calls += 1
        raise ContinuationUnrecoverableError(
            "stale codex session", service_name="codex"
        )

    async def run_new_session(self, request):
        del request
        self.run_new_session_calls += 1
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


def _make_stale_continuation_runner(tmp_path, *, issue: int):
    mount_path = tmp_path / "repo" / "pycastle" / ".worktrees" / f"issue-{issue}"
    mount_path.mkdir(parents=True)
    session_dir = mount_path / ".pycastle-session" / "implementer"
    session_dir.mkdir(parents=True)
    (session_dir / "_continuation").write_text(
        _VALID_STALE_CONTINUATION, encoding="utf-8"
    )
    git_service = MagicMock(spec=GitService)
    git_service.get_user_name.return_value = "Test User"
    git_service.get_user_email.return_value = "test@example.com"
    git_service.is_working_tree_clean.return_value = True
    runner = AgentRunner(
        env={},
        cfg=Config(logs_dir=tmp_path / "logs"),
        git_service=git_service,
        service_registry={"codex": _FakeService()},
    )
    return runner, mount_path, session_dir, git_service


def _base_scope_args(issue: int) -> dict:
    return {
        "ISSUE_NUMBER": str(issue),
        "ISSUE_TITLE": "Fix stale continuation",
        "ISSUE_BODY": "",
        "ISSUE_COMMENTS": "",
        "BRANCH": f"issue-{issue}",
        "INTERRUPTED_WORK": "",
        "OPERATING_BRANCH": "main",
    }


def test_stale_continuation_fresh_retry_succeeds_on_unrecoverable_error(
    tmp_path, monkeypatch
):
    runner, mount_path, session_dir, _git = _make_stale_continuation_runner(
        tmp_path, issue=1939
    )
    runtime_client = _StaleResumeRuntimeClient()
    status_display = RecordingStatusDisplay()

    monkeypatch.setattr(
        runner, "_build_session", lambda *_args, **_kwargs: _FakeDockerSession()
    )
    monkeypatch.setattr(
        "pycastle.agents.runner.render_prompt_invocation",
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
                name="Implement Agent #1939",
                prompt=PromptInvocation(
                    template=PromptTemplate.IMPLEMENT_BEHAVIOR,
                    scope_args=_base_scope_args(1939),
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
    assert not (session_dir / "_continuation").is_file()
    assert runtime_client.run_resumed_session_calls == 1
    assert runtime_client.run_new_session_calls == 1


def test_stale_continuation_fresh_retry_sets_interrupted_work_on_dirty_tree(
    tmp_path, monkeypatch
):
    runner, mount_path, _session_dir, git_service = _make_stale_continuation_runner(
        tmp_path, issue=1939
    )
    git_service.is_working_tree_clean.return_value = False
    runtime_client = _StaleResumeRuntimeClient()
    status_display = RecordingStatusDisplay()
    render_calls: list[dict] = []

    async def recording_render(invocation, *, renderer, run_kind, exec_fn):
        render_calls.append(
            {
                "run_kind": run_kind,
                "interrupted_work": invocation.scope_args.get("INTERRUPTED_WORK", ""),
            }
        )
        return "prompt"

    monkeypatch.setattr(
        runner, "_build_session", lambda *_args, **_kwargs: _FakeDockerSession()
    )
    monkeypatch.setattr(
        "pycastle.agents.runner.render_prompt_invocation", recording_render
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
                name="Implement Agent #1939",
                prompt=PromptInvocation(
                    template=PromptTemplate.IMPLEMENT_BEHAVIOR,
                    scope_args=_base_scope_args(1939),
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
    assert len(render_calls) == 2
    assert render_calls[0]["interrupted_work"] == ""
    assert render_calls[1]["interrupted_work"] != ""
    assert render_calls[1]["run_kind"].value == "fresh"


def test_stale_continuation_proactive_service_mismatch_skips_resumed_session(
    tmp_path, monkeypatch
):
    mount_path = tmp_path / "repo" / "pycastle" / ".worktrees" / "issue-1940"
    mount_path.mkdir(parents=True)
    session_dir = mount_path / ".pycastle-session" / "implementer"
    session_dir.mkdir(parents=True)
    (session_dir / "_continuation").write_text(
        _VALID_STALE_CONTINUATION, encoding="utf-8"
    )
    codex_state_dir = session_dir / "codex"
    codex_state_dir.mkdir(parents=True)
    (codex_state_dir / "thread_id").write_text("codex-session-id", encoding="utf-8")

    git_service = MagicMock(spec=GitService)
    git_service.get_user_name.return_value = "Test User"
    git_service.get_user_email.return_value = "test@example.com"
    git_service.is_working_tree_clean.return_value = True

    opencode_service = _RecordingService("opencode")
    runner = AgentRunner(
        env={},
        cfg=Config(logs_dir=tmp_path / "logs"),
        git_service=git_service,
        service_registry={"opencode": opencode_service},
    )
    status_display = RecordingStatusDisplay()

    resumed_session_calls = []

    class _ServiceMismatchRuntimeClient:
        async def run_resumed_session(self, request):
            resumed_session_calls.append(request)
            raise AssertionError(
                "run_resumed_session must not be called on service mismatch"
            )

        async def run_new_session(self, request):
            del request
            return RuntimeOutcome(
                kind=Completed(),
                result=RunResult(
                    output="<commit_message>done</commit_message>",
                    usage=None,
                    continuation=None,
                    selected=ResolvedProvider(
                        service="opencode",
                        model="gpt-5.5",
                        effort="medium",
                    ),
                ),
            )

    monkeypatch.setattr(
        runner, "_build_session", lambda *_args, **_kwargs: _FakeDockerSession()
    )
    monkeypatch.setattr(
        "pycastle.agents.runner.render_prompt_invocation",
        AsyncMock(return_value="prompt"),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner.setup",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner._get_runtime_client",
        lambda _self: _ServiceMismatchRuntimeClient(),
    )

    result = asyncio.run(
        runner.run(
            RunRequest(
                name="Implement Agent #1940",
                prompt=PromptInvocation(
                    template=PromptTemplate.IMPLEMENT_BEHAVIOR,
                    scope_args={
                        "ISSUE_NUMBER": "1940",
                        "ISSUE_TITLE": "Service switched to opencode",
                        "ISSUE_BODY": "",
                        "ISSUE_COMMENTS": "",
                        "BRANCH": "issue-1940",
                        "INTERRUPTED_WORK": "",
                        "OPERATING_BRANCH": "main",
                    },
                ),
                mount_path=mount_path,
                role=AgentRole.IMPLEMENTER,
                model="gpt-5.5",
                effort="medium",
                service="opencode",
                status_display=status_display,
            )
        )
    )

    assert isinstance(result, CommitMessageOutput)
    assert resumed_session_calls == []
    assert not (session_dir / "_continuation").is_file()


def test_stale_continuation_proactive_service_mismatch_sets_interrupted_work_on_dirty_tree(
    tmp_path, monkeypatch
):
    mount_path = tmp_path / "repo" / "pycastle" / ".worktrees" / "issue-1940"
    mount_path.mkdir(parents=True)
    session_dir = mount_path / ".pycastle-session" / "implementer"
    session_dir.mkdir(parents=True)
    (session_dir / "_continuation").write_text(
        _VALID_STALE_CONTINUATION, encoding="utf-8"
    )
    codex_state_dir = session_dir / "codex"
    codex_state_dir.mkdir(parents=True)
    (codex_state_dir / "thread_id").write_text("codex-session-id", encoding="utf-8")

    git_service = MagicMock(spec=GitService)
    git_service.get_user_name.return_value = "Test User"
    git_service.get_user_email.return_value = "test@example.com"
    git_service.is_working_tree_clean.return_value = False

    opencode_service = _RecordingService("opencode")
    runner = AgentRunner(
        env={},
        cfg=Config(logs_dir=tmp_path / "logs"),
        git_service=git_service,
        service_registry={"opencode": opencode_service},
    )
    status_display = RecordingStatusDisplay()
    render_calls: list[dict] = []

    async def recording_render(invocation, *, renderer, run_kind, exec_fn):
        render_calls.append(
            {
                "run_kind": run_kind,
                "interrupted_work": invocation.scope_args.get("INTERRUPTED_WORK", ""),
            }
        )
        return "prompt"

    class _NewSessionOnlyRuntimeClient:
        async def run_new_session(self, request):
            del request
            return RuntimeOutcome(
                kind=Completed(),
                result=RunResult(
                    output="<commit_message>done</commit_message>",
                    usage=None,
                    continuation=None,
                    selected=ResolvedProvider(
                        service="opencode",
                        model="gpt-5.5",
                        effort="medium",
                    ),
                ),
            )

    monkeypatch.setattr(
        runner, "_build_session", lambda *_args, **_kwargs: _FakeDockerSession()
    )
    monkeypatch.setattr(
        "pycastle.agents.runner.render_prompt_invocation", recording_render
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner.setup",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner._get_runtime_client",
        lambda _self: _NewSessionOnlyRuntimeClient(),
    )

    result = asyncio.run(
        runner.run(
            RunRequest(
                name="Implement Agent #1940",
                prompt=PromptInvocation(
                    template=PromptTemplate.IMPLEMENT_BEHAVIOR,
                    scope_args={
                        "ISSUE_NUMBER": "1940",
                        "ISSUE_TITLE": "Service switched to opencode",
                        "ISSUE_BODY": "",
                        "ISSUE_COMMENTS": "",
                        "BRANCH": "issue-1940",
                        "INTERRUPTED_WORK": "",
                        "OPERATING_BRANCH": "main",
                    },
                ),
                mount_path=mount_path,
                role=AgentRole.IMPLEMENTER,
                model="gpt-5.5",
                effort="medium",
                service="opencode",
                status_display=status_display,
            )
        )
    )

    assert isinstance(result, CommitMessageOutput)
    assert len(render_calls) == 2
    assert render_calls[0]["interrupted_work"] == ""
    assert render_calls[1]["interrupted_work"] != ""
    assert render_calls[1]["run_kind"].value == "fresh"


class _ExhaustableService(_FakeService):
    """Service that becomes unavailable after mark_exhausted is called."""

    def __init__(self, name: str = "codex") -> None:
        self.name = name
        self.mark_exhausted_calls: list[object] = []
        self._exhausted = False

    def is_available(self, now=None, *, model=None) -> bool:
        del now, model
        return not self._exhausted

    def mark_exhausted(self, reset_time, *, _now=None) -> None:
        del _now
        self.mark_exhausted_calls.append(reset_time)
        self._exhausted = True

    def mark_permanently_exhausted(self) -> str | None:
        self._exhausted = True
        return "exhausted-account"


class _UsageLimitedRuntimeClient:
    async def run_new_session(self, request):
        del request
        return RuntimeOutcome(
            kind=UsageLimited(reset_time=None),
            result=RunResult(
                output="",
                usage=None,
                continuation=None,
                selected=ResolvedProvider(
                    service="codex",
                    model="gpt-5.5",
                    effort="medium",
                ),
            ),
        )


class _PermanentUsageLimitedRuntimeClient:
    async def run_new_session(self, request):
        del request
        return RuntimeOutcome(
            kind=UsageLimited(reset_time=None, is_permanent=True),
            result=RunResult(
                output="",
                usage=None,
                continuation=None,
                selected=ResolvedProvider(
                    service="opencode",
                    model="open-code",
                    effort="medium",
                ),
            ),
        )


class _ServiceNotAvailableRuntimeClient:
    async def run_new_session(self, request):
        del request
        return RuntimeOutcome(
            kind=ProviderUnavailable(
                reason=ProviderUnavailableReason.SERVICE_NOT_AVAILABLE,
                detail="account exhausted",
            ),
            result=RunResult(
                output="",
                usage=None,
                continuation=None,
                selected=ResolvedProvider(
                    service="codex",
                    model="gpt-5.5",
                    effort="medium",
                ),
            ),
        )


class _HardErrorRuntimeClient:
    async def run_new_session(self, request):
        del request
        raise HardAgentError("fatal runtime error")


class _OpenCodeCredentialFailureRuntimeClient:
    async def run_new_session(self, request):
        del request
        return RuntimeOutcome(
            kind=UsageLimited(reset_time=None, is_permanent=True),
            result=RunResult(
                output="",
                usage=None,
                continuation=None,
                selected=ResolvedProvider(
                    service="opencode",
                    model="open-code",
                    effort="medium",
                ),
            ),
        )


class _NonOpenCodeCredentialFailureRuntimeClient:
    async def run_new_session(self, request):
        del request
        raise AgentCredentialFailureError("credentials expired", service_name="codex")


class _TransientApiErrorRuntimeClient:
    async def run_new_session(self, request):
        del request
        return RuntimeOutcome(
            kind=ProviderUnavailable(
                reason=ProviderUnavailableReason.TRANSIENT_API_ERROR,
                detail="upstream timeout",
            ),
            result=RunResult(
                output="",
                usage=None,
                continuation=None,
                selected=ResolvedProvider(
                    service="codex",
                    model="gpt-5.5",
                    effort="medium",
                ),
            ),
        )


class _ModelNotAvailableRuntimeClient:
    async def run_new_session(self, request):
        del request
        return RuntimeOutcome(
            kind=ModelNotAvailable(),
            result=RunResult(
                output="",
                usage=None,
                continuation=None,
                selected=ResolvedProvider(
                    service="codex",
                    model="gpt-5.5",
                    effort="medium",
                ),
            ),
        )


def _setup_runner_for_token_tests(
    tmp_path,
    monkeypatch,
    *,
    service,
    runtime_client,
    issue: int,
):
    mount_path = tmp_path / "repo" / "pycastle" / ".worktrees" / f"issue-{issue}"
    mount_path.mkdir(parents=True)

    git_service = MagicMock(spec=GitService)
    git_service.get_user_name.return_value = "Test User"
    git_service.get_user_email.return_value = "test@example.com"

    runner = AgentRunner(
        env={},
        cfg=Config(logs_dir=tmp_path / "logs"),
        git_service=git_service,
        service_registry={service.name: service},
    )

    monkeypatch.setattr(
        runner, "_build_session", lambda *_args, **_kwargs: _FakeDockerSession()
    )
    monkeypatch.setattr(
        "pycastle.agents.runner.render_prompt_invocation",
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
    return runner, mount_path


def _make_implement_request(mount_path, service_name, issue, *, token=None):
    return RunRequest(
        name=f"Implement Agent #{issue}",
        prompt=PromptInvocation(
            template=PromptTemplate.IMPLEMENT_BEHAVIOR,
            scope_args={
                "ISSUE_NUMBER": str(issue),
                "ISSUE_TITLE": "Test issue",
                "ISSUE_BODY": "",
                "ISSUE_COMMENTS": "",
                "BRANCH": f"issue-{issue}",
                "INTERRUPTED_WORK": "",
                "OPERATING_BRANCH": "main",
            },
        ),
        mount_path=mount_path,
        role=AgentRole.IMPLEMENTER,
        model="gpt-5.5",
        effort="medium",
        service=service_name,
        token=token,
    )


class _SessionStoreCapturingRuntimeClient:
    def __init__(self) -> None:
        self.session_store: Path | None = None

    async def run_new_session(self, request):
        self.session_store = request.session_store
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


class _ProviderStateDirService(_FakeService):
    def state_dir_relpath(self, role, namespace: str = "") -> str | None:
        del role, namespace
        return ".pycastle-session/implementer/codex/"


def test_agent_runner_uses_provider_state_dir_as_runtime_session_store(
    tmp_path,
    monkeypatch,
):
    # Regression for #1954: ar 2.4 probes `session_store` directly for the
    # provider transcript, so pycastle must pass the per-provider state dir
    # (where CLAUDE_CONFIG_DIR/CODEX_HOME point) rather than the bare role
    # session path. Otherwise ar probes an always-empty dir, downgrades
    # RESUME->FRESH, and reuses the session id -> "Session ID ... is already in use".
    repo_root = tmp_path / "repo"
    mount_path = repo_root / "pycastle" / ".worktrees" / "issue-1954"
    mount_path.mkdir(parents=True)

    git_service = MagicMock(spec=GitService)
    git_service.get_user_name.return_value = "Test User"
    git_service.get_user_email.return_value = "test@example.com"
    runner = AgentRunner(
        env={},
        cfg=Config(logs_dir=tmp_path / "logs"),
        git_service=git_service,
        service_registry={"codex": _ProviderStateDirService()},
    )
    runtime_client = _SessionStoreCapturingRuntimeClient()
    status_display = RecordingStatusDisplay()

    monkeypatch.setattr(
        runner, "_build_session", lambda *_args, **_kwargs: _FakeDockerSession()
    )
    monkeypatch.setattr(
        "pycastle.agents.runner.render_prompt_invocation",
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

    asyncio.run(
        runner.run(
            RunRequest(
                name="Implement Agent #1954",
                prompt=PromptInvocation(
                    template=PromptTemplate.IMPLEMENT_BEHAVIOR,
                    scope_args={
                        "ISSUE_NUMBER": "1954",
                        "ISSUE_TITLE": "Align ar session store with provider state dir",
                        "ISSUE_BODY": "",
                        "ISSUE_COMMENTS": "",
                        "BRANCH": "issue-1954",
                        "INTERRUPTED_WORK": "",
                        "OPERATING_BRANCH": "main",
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

    assert runtime_client.session_store == (
        mount_path / ".pycastle-session" / "implementer" / "codex"
    )


def test_agent_runner_model_not_available_records_restriction_and_raises(
    tmp_path,
    monkeypatch,
):
    mount_path = tmp_path / "repo" / "pycastle" / ".worktrees" / "issue-1952"
    mount_path.mkdir(parents=True)

    git_service = MagicMock(spec=GitService)
    git_service.get_user_name.return_value = "Test User"
    git_service.get_user_email.return_value = "test@example.com"
    service = _RecordingService("codex")
    runner = AgentRunner(
        env={},
        cfg=Config(logs_dir=tmp_path / "logs"),
        git_service=git_service,
        service_registry={"codex": service},
    )
    status_display = RecordingStatusDisplay()

    class _ModelNotAvailableRuntimeClient:
        async def run_new_session(self, request):
            return RuntimeOutcome(
                kind=ModelNotAvailable(),
                result=RunResult(
                    output="",
                    usage=None,
                    continuation=None,
                    selected=ResolvedProvider(
                        service="codex",
                        model="gpt-5.5",
                        effort="medium",
                    ),
                ),
            )

    runtime_client = _ModelNotAvailableRuntimeClient()

    monkeypatch.setattr(
        runner, "_build_session", lambda *_args, **_kwargs: _FakeDockerSession()
    )
    monkeypatch.setattr(
        "pycastle.agents.runner.render_prompt_invocation",
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

    with pytest.raises(ModelNotAvailableError) as excinfo:
        asyncio.run(
            runner.run(
                RunRequest(
                    name="Implement Agent #1952",
                    prompt=PromptInvocation(
                        template=PromptTemplate.IMPLEMENT_BEHAVIOR,
                        scope_args={
                            "ISSUE_NUMBER": "1952",
                            "ISSUE_TITLE": "Handle ModelNotAvailable without crashing",
                            "ISSUE_BODY": "",
                            "ISSUE_COMMENTS": "",
                            "BRANCH": "issue-1952",
                            "INTERRUPTED_WORK": "",
                            "OPERATING_BRANCH": "main",
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

    assert excinfo.value.service == "codex"
    assert excinfo.value.model == "gpt-5.5"
    assert excinfo.value.stage_key == "implement"
    assert not isinstance(excinfo.value, UsageLimitError)
    assert service.mark_model_restricted_calls == ["gpt-5.5"]
    assert service.mark_exhausted_calls == []


def test_improve_same_run_phase2_resumes_phase1_session(tmp_path, monkeypatch):
    """Spec Agent must resume the Scan Agent's conversation in a same-run sequence.

    When phase 1 (Scan Agent) and phase 2 (Spec Agent) run sequentially inside the
    same pycastle invocation, the runner must preserve the _continuation so phase 2
    calls run_resumed_session — giving the Spec Agent access to the Scan Agent's
    codebase exploration without re-scanning.

    Reproduces: Spec Agent saying "the prior phase-1 output isn't persisted anywhere I
    can find" because clear_provider_state_and_signal_completion() deletes _continuation
    after phase 1, forcing phase 2 into a fresh session with no prior context.
    """
    mount_path = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
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

    class _Phase1ThenResumeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def run_new_session(self, request):
            del request
            self.calls.append("new_session")
            return RuntimeOutcome(
                kind=Completed(),
                result=RunResult(
                    output="<promise>COMPLETE</promise>",
                    usage=None,
                    continuation=Continuation(serialized=_VALID_STALE_CONTINUATION),
                    selected=ResolvedProvider(
                        service="codex",
                        model="gpt-5.5",
                        effort="medium",
                    ),
                ),
            )

        async def run_resumed_session(self, request):
            del request
            self.calls.append("resumed_session")
            return RuntimeOutcome(
                kind=Completed(),
                result=RunResult(
                    output="<promise>COMPLETE</promise>",
                    usage=None,
                    continuation=None,
                    selected=ResolvedProvider(
                        service="codex",
                        model="gpt-5.5",
                        effort="medium",
                    ),
                ),
            )

    runtime_client = _Phase1ThenResumeClient()
    status_display = RecordingStatusDisplay()

    monkeypatch.setattr(
        runner, "_build_session", lambda *_args, **_kwargs: _FakeDockerSession()
    )
    monkeypatch.setattr(
        "pycastle.agents.runner.render_prompt_invocation",
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

    asyncio.run(
        runner.run(
            RunRequest(
                name="Scan Agent",
                prompt=PromptInvocation(
                    template=PromptTemplate.IMPROVE_SCAN,
                    scope_args={
                        "RECENT_IMPROVE_SPEC_TITLES": "",
                        "CANDIDATE_BUDGET": "3",
                    },
                ),
                mount_path=mount_path,
                role=AgentRole.IMPROVE,
                model="gpt-5.5",
                effort="medium",
                service="codex",
                session_namespace="main",
                status_display=status_display,
                preserve_session_on_completion=True,
            )
        )
    )

    asyncio.run(
        runner.run(
            RunRequest(
                name="Spec Agent",
                prompt=PromptInvocation(
                    template=PromptTemplate.IMPROVE_SPEC,
                    scope_args={
                        "IMPROVE_SHORT_SID": "abc123",
                        "RECENT_IMPROVE_SPECS": "",
                        "CANDIDATE_RANK": "1",
                        "CANDIDATE_TITLE": "Deepen the parser module",
                    },
                ),
                mount_path=mount_path,
                role=AgentRole.IMPROVE,
                model="gpt-5.5",
                effort="medium",
                service="codex",
                session_namespace="main",
                status_display=status_display,
                preserve_session_on_completion=True,
            )
        )
    )

    assert runtime_client.calls == ["new_session", "resumed_session"], (
        f"Expected phase 2 to resume phase 1's session but got: {runtime_client.calls}. "
        "Spec Agent must call run_resumed_session to continue Scan Agent's conversation."
    )


# ── ADR 0054: sibling-agent cancellation policy ──────────────────────────────


def test_usage_limited_outcome_does_not_cancel_shared_token(tmp_path, monkeypatch):
    service = _ExhaustableService("codex")
    runner, mount_path = _setup_runner_for_token_tests(
        tmp_path,
        monkeypatch,
        service=service,
        runtime_client=_UsageLimitedRuntimeClient(),
        issue=2054,
    )
    token = CancellationToken()

    with pytest.raises(UsageLimitError):
        asyncio.run(
            runner.run(_make_implement_request(mount_path, "codex", 2054, token=token))
        )

    assert not token.is_cancelled
    assert service.mark_exhausted_calls


def test_permanent_usage_limited_outcome_calls_mark_permanently_exhausted(
    tmp_path, monkeypatch
):
    service = _ExhaustableService("opencode")
    runner, mount_path = _setup_runner_for_token_tests(
        tmp_path,
        monkeypatch,
        service=service,
        runtime_client=_PermanentUsageLimitedRuntimeClient(),
        issue=2054,
    )
    token = CancellationToken()

    with pytest.raises(UsageLimitError) as excinfo:
        asyncio.run(
            runner.run(
                _make_implement_request(mount_path, "opencode", 2054, token=token)
            )
        )

    assert excinfo.value.is_permanent is True
    assert not token.is_cancelled
    assert not service.is_available()
    assert not service.mark_exhausted_calls


def test_opencode_credential_failure_does_not_cancel_shared_token(
    tmp_path, monkeypatch
):
    service = _ExhaustableService("opencode")
    runner, mount_path = _setup_runner_for_token_tests(
        tmp_path,
        monkeypatch,
        service=service,
        runtime_client=_OpenCodeCredentialFailureRuntimeClient(),
        issue=2054,
    )
    token = CancellationToken()

    with pytest.raises(UsageLimitError) as excinfo:
        asyncio.run(
            runner.run(
                _make_implement_request(mount_path, "opencode", 2054, token=token)
            )
        )

    assert excinfo.value.is_permanent is True
    assert not token.is_cancelled
    assert not service.is_available()


def test_non_opencode_credential_failure_does_not_cancel_shared_token(
    tmp_path, monkeypatch
):
    service = _ExhaustableService("codex")
    runner, mount_path = _setup_runner_for_token_tests(
        tmp_path,
        monkeypatch,
        service=service,
        runtime_client=_NonOpenCodeCredentialFailureRuntimeClient(),
        issue=2054,
    )
    token = CancellationToken()

    with pytest.raises(AgentCredentialFailureError):
        asyncio.run(
            runner.run(_make_implement_request(mount_path, "codex", 2054, token=token))
        )

    assert not token.is_cancelled


def test_hard_agent_error_does_not_cancel_shared_token(tmp_path, monkeypatch):
    service = _ExhaustableService("codex")
    runner, mount_path = _setup_runner_for_token_tests(
        tmp_path,
        monkeypatch,
        service=service,
        runtime_client=_HardErrorRuntimeClient(),
        issue=2054,
    )
    token = CancellationToken()

    with pytest.raises(HardAgentError):
        asyncio.run(
            runner.run(_make_implement_request(mount_path, "codex", 2054, token=token))
        )

    assert not token.is_cancelled


def test_transient_api_error_does_not_cancel_shared_token(tmp_path, monkeypatch):
    service = _ExhaustableService("codex")
    runner, mount_path = _setup_runner_for_token_tests(
        tmp_path,
        monkeypatch,
        service=service,
        runtime_client=_TransientApiErrorRuntimeClient(),
        issue=2054,
    )
    token = CancellationToken()

    with pytest.raises(TransientAgentError):
        asyncio.run(
            runner.run(_make_implement_request(mount_path, "codex", 2054, token=token))
        )

    assert not token.is_cancelled


def test_model_not_available_does_not_cancel_shared_token(tmp_path, monkeypatch):
    service = _ExhaustableService("codex")
    runner, mount_path = _setup_runner_for_token_tests(
        tmp_path,
        monkeypatch,
        service=service,
        runtime_client=_ModelNotAvailableRuntimeClient(),
        issue=2054,
    )
    token = CancellationToken()

    with pytest.raises(ModelNotAvailableError):
        asyncio.run(
            runner.run(_make_implement_request(mount_path, "codex", 2054, token=token))
        )

    assert not token.is_cancelled


def test_early_guard_fires_on_unavailable_service_without_cancelled_token(
    tmp_path, monkeypatch
):
    service = _ExhaustableService("codex")
    service.mark_exhausted(None)  # exhaust before the agent even starts

    class _NeverCalledRuntimeClient:
        async def run_new_session(self, request):
            raise AssertionError(
                "runtime must not be reached when service is unavailable"
            )

    runner, mount_path = _setup_runner_for_token_tests(
        tmp_path,
        monkeypatch,
        service=service,
        runtime_client=_NeverCalledRuntimeClient(),
        issue=2054,
    )
    token = CancellationToken()
    assert not token.is_cancelled

    with pytest.raises(UsageLimitError):
        asyncio.run(
            runner.run(_make_implement_request(mount_path, "codex", 2054, token=token))
        )

    assert not token.is_cancelled


def test_opencode_timeout_does_not_cancel_shared_token(tmp_path, monkeypatch):
    service = _ExhaustableService("opencode")
    continuation = Continuation(serialized="opaque-continuation")
    runner, mount_path = _setup_runner_for_token_tests(
        tmp_path,
        monkeypatch,
        service=service,
        runtime_client=_TimedOutRuntimeClient(continuation),
        issue=2054,
    )
    token = CancellationToken()
    monkeypatch.setattr(
        "pycastle.agents.runner._time_module.now_local",
        lambda: datetime(2026, 6, 27, 12, 30, tzinfo=UTC),
    )

    with pytest.raises(UsageLimitError):
        asyncio.run(
            runner.run(
                _make_implement_request(mount_path, "opencode", 2054, token=token)
            )
        )

    assert not token.is_cancelled
    assert service.mark_exhausted_calls


def test_provider_unavailable_outcome_does_not_cancel_shared_token(
    tmp_path, monkeypatch
):
    service = _ExhaustableService("codex")
    runner, mount_path = _setup_runner_for_token_tests(
        tmp_path,
        monkeypatch,
        service=service,
        runtime_client=_ServiceNotAvailableRuntimeClient(),
        issue=2054,
    )
    token = CancellationToken()

    with pytest.raises(UsageLimitError):
        asyncio.run(
            runner.run(_make_implement_request(mount_path, "codex", 2054, token=token))
        )

    assert not token.is_cancelled
    assert service.mark_exhausted_calls


# ── Issue 2007: BLE001 — blind-except narrowing ───────────────────────────────


class _ExplodingExitDockerSession(_FakeDockerSession):
    """Session whose __exit__ raises RuntimeError to verify teardown errors propagate."""

    def __exit__(self, *_args) -> None:
        raise RuntimeError("session teardown failed")


class _FailingValidModelsService(_FakeService):
    """Service whose valid_models() raises to verify the error propagates."""

    def valid_models(self) -> frozenset[str]:
        raise ValueError("models unavailable")


def test_default_model_propagates_valid_models_error(tmp_path):
    mount_path = tmp_path / "repo" / "pycastle" / ".worktrees" / "issue-2007"
    mount_path.mkdir(parents=True)

    git_service = MagicMock(spec=GitService)
    git_service.get_user_name.return_value = "Test User"
    git_service.get_user_email.return_value = "test@example.com"
    runner = AgentRunner(
        env={},
        cfg=Config(logs_dir=tmp_path / "logs"),
        git_service=git_service,
        service_registry={"codex": _FailingValidModelsService()},
    )

    with pytest.raises(ValueError, match="models unavailable"):
        asyncio.run(
            runner.run(
                RunRequest(
                    name="Test Agent",
                    prompt=PromptInvocation(
                        template=PromptTemplate.IMPLEMENT_BEHAVIOR,
                        scope_args={
                            "ISSUE_NUMBER": "2007",
                            "ISSUE_TITLE": "Test",
                            "ISSUE_BODY": "",
                            "ISSUE_COMMENTS": "",
                            "BRANCH": "issue-2007",
                            "INTERRUPTED_WORK": "",
                            "OPERATING_BRANCH": "main",
                        },
                    ),
                    mount_path=mount_path,
                    role=AgentRole.IMPLEMENTER,
                    model="",
                    effort="medium",
                    service="codex",
                )
            )
        )


def test_run_propagates_non_docker_exceptions_from_session_build(tmp_path, monkeypatch):
    mount_path = tmp_path / "repo" / "pycastle" / ".worktrees" / "issue-2007"
    mount_path.mkdir(parents=True)

    git_service = MagicMock(spec=GitService)
    git_service.get_user_name.return_value = "Test User"
    git_service.get_user_email.return_value = "test@example.com"
    runner = AgentRunner(
        env={},
        cfg=Config(logs_dir=tmp_path / "logs"),
        git_service=git_service,
        service_registry={"codex": _FakeService()},
        docker_client=MagicMock(),
    )

    def _exploding_init(self, **_kw):
        raise ValueError("non-docker init failure")

    monkeypatch.setattr(
        "pycastle.infrastructure.docker_session.DockerSession.__init__",
        _exploding_init,
    )

    with pytest.raises(ValueError, match="non-docker init failure"):
        asyncio.run(
            runner.run(
                RunRequest(
                    name="Test Agent",
                    prompt=PromptInvocation(
                        template=PromptTemplate.IMPLEMENT_BEHAVIOR,
                        scope_args={
                            "ISSUE_NUMBER": "2007",
                            "ISSUE_TITLE": "Test",
                            "ISSUE_BODY": "",
                            "ISSUE_COMMENTS": "",
                            "BRANCH": "issue-2007",
                            "INTERRUPTED_WORK": "",
                            "OPERATING_BRANCH": "main",
                        },
                    ),
                    mount_path=mount_path,
                    role=AgentRole.IMPLEMENTER,
                    model="gpt-5.5",
                    effort="medium",
                    service="codex",
                )
            )
        )


def test_run_propagates_non_oserror_from_session_exit(tmp_path, monkeypatch):
    mount_path = tmp_path / "repo" / "pycastle" / ".worktrees" / "issue-2007"
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

    monkeypatch.setattr(
        runner, "_build_session", lambda *_a, **_kw: _ExplodingExitDockerSession()
    )
    monkeypatch.setattr(
        "pycastle.agents.runner.render_prompt_invocation",
        AsyncMock(return_value="prompt"),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner.setup",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner._get_runtime_client",
        lambda _self: _FakeRuntimeClient(object()),
    )

    with pytest.raises(RuntimeError, match="session teardown failed"):
        asyncio.run(
            runner.run(
                RunRequest(
                    name="Test Agent",
                    prompt=PromptInvocation(
                        template=PromptTemplate.IMPLEMENT_BEHAVIOR,
                        scope_args={
                            "ISSUE_NUMBER": "2007",
                            "ISSUE_TITLE": "Test",
                            "ISSUE_BODY": "",
                            "ISSUE_COMMENTS": "",
                            "BRANCH": "issue-2007",
                            "INTERRUPTED_WORK": "",
                            "OPERATING_BRANCH": "main",
                        },
                    ),
                    mount_path=mount_path,
                    role=AgentRole.IMPLEMENTER,
                    model="gpt-5.5",
                    effort="medium",
                    service="codex",
                )
            )
        )


class _CapturingToolPolicyRuntimeClient:
    def __init__(self) -> None:
        self.captured_tool_policy: ToolPolicy | None = None

    async def run_new_session(self, request):
        self.captured_tool_policy = request.tool_policy
        return RuntimeOutcome(
            kind=Completed(),
            result=RunResult(
                output="<promise>COMPLETE</promise>",
                usage=None,
                continuation=None,
                selected=ResolvedProvider(
                    service="codex",
                    model="gpt-5.5",
                    effort="medium",
                ),
            ),
        )


def _make_runner_for_policy_test(tmp_path, monkeypatch, *, issue: int, runtime_client):
    mount_path = tmp_path / "repo" / "pycastle" / ".worktrees" / f"issue-{issue}"
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

    monkeypatch.setattr(
        runner, "_build_session", lambda *_args, **_kwargs: _FakeDockerSession()
    )
    monkeypatch.setattr(
        "pycastle.agents.runner.render_prompt_invocation",
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
    return runner, mount_path


def test_divergence_resolver_runs_with_unrestricted_tool_policy(tmp_path, monkeypatch):
    runtime_client = _CapturingToolPolicyRuntimeClient()
    runner, mount_path = _make_runner_for_policy_test(
        tmp_path, monkeypatch, issue=2171, runtime_client=runtime_client
    )

    asyncio.run(
        runner.run(
            RunRequest(
                name="Divergence Resolver #2171",
                prompt=PromptInvocation(
                    template=PromptTemplate.DIVERGENCE_RESOLVE,
                    scope_args={"BRANCH": "issue-2171"},
                ),
                mount_path=mount_path,
                role=AgentRole.DIVERGENCE_RESOLVER,
                model="gpt-5.5",
                effort="medium",
                service="codex",
            )
        )
    )

    assert runtime_client.captured_tool_policy == ToolPolicy.UNRESTRICTED


def test_planner_keeps_no_file_mutation_tool_policy(tmp_path, monkeypatch):
    runtime_client = _CapturingToolPolicyRuntimeClient()
    runner, mount_path = _make_runner_for_policy_test(
        tmp_path, monkeypatch, issue=2171, runtime_client=runtime_client
    )
    monkeypatch.setattr(
        "pycastle.prompts.pipeline.PromptRenderer.render_expected_output_shape",
        lambda self, template, scope_args: "<plan>{...}</plan>",
    )

    class _PlannerSuccessRuntimeClient:
        async def run_new_session(self, request):
            runtime_client.captured_tool_policy = request.tool_policy
            return RuntimeOutcome(
                kind=Completed(),
                result=RunResult(
                    output='<plan>{"issues": [], "blocked": []}</plan>',
                    usage=None,
                    continuation=None,
                    selected=ResolvedProvider(
                        service="codex",
                        model="gpt-5.5",
                        effort="medium",
                    ),
                ),
            )

    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner._get_runtime_client",
        lambda _self: _PlannerSuccessRuntimeClient(),
    )

    asyncio.run(
        runner.run(
            RunRequest(
                name="Plan Agent",
                prompt=PromptInvocation(
                    template=PromptTemplate.PLAN,
                    scope_args={
                        "ALL_OPEN_ISSUES_JSON": "[]",
                        "READY_FOR_AGENT_ISSUES_JSON": "[]",
                    },
                ),
                mount_path=mount_path,
                role=AgentRole.PLANNER,
                model="gpt-5.5",
                effort="medium",
                service="codex",
            )
        )
    )

    assert runtime_client.captured_tool_policy == ToolPolicy.NO_FILE_MUTATION


def test_run_preflight_propagates_non_oserror_from_session_exit(tmp_path, monkeypatch):
    mount_path = tmp_path / "repo" / "pycastle" / ".worktrees" / "issue-2007"
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

    monkeypatch.setattr(
        runner,
        "_build_preflight_session",
        lambda *_a, **_kw: _ExplodingExitDockerSession(),
    )

    with pytest.raises(RuntimeError, match="session teardown failed"):
        asyncio.run(
            runner.run_preflight(
                name="Preflight Agent",
                mount_path=mount_path,
            )
        )


# ── Issue 2252: proactive service-mismatch via transcript ownership ───────────


def _make_session_with_transcript_owner(
    tmp_path,
    *,
    issue: int,
    owner_service: str,
    dispatch_service: str,
    is_working_tree_clean: bool = True,
) -> tuple:
    """Return (runner, mount_path, session_dir, git_service) for mismatch tests.

    Signals transcript ownership through directory layout: creates
    ``<session>/<owner_service>/thread_id`` so ``transcript_owner_service_name``
    returns the owner's name without relying on the dead JSON sidecar.
    """
    mount_path = tmp_path / "repo" / "pycastle" / ".worktrees" / f"issue-{issue}"
    mount_path.mkdir(parents=True)
    session_dir = mount_path / ".pycastle-session" / "implementer"
    session_dir.mkdir(parents=True)
    (session_dir / "_continuation").write_text(
        _VALID_STALE_CONTINUATION, encoding="utf-8"
    )
    owner_state_dir = session_dir / owner_service
    owner_state_dir.mkdir(parents=True)
    (owner_state_dir / "thread_id").write_text("owner-session-id", encoding="utf-8")

    git_service = MagicMock(spec=GitService)
    git_service.get_user_name.return_value = "Test User"
    git_service.get_user_email.return_value = "test@example.com"
    git_service.is_working_tree_clean.return_value = is_working_tree_clean

    runner = AgentRunner(
        env={},
        cfg=Config(logs_dir=tmp_path / "logs"),
        git_service=git_service,
        service_registry={dispatch_service: _RecordingService(dispatch_service)},
    )
    return runner, mount_path, session_dir, git_service


def test_cross_service_fallback_with_transcript_owner_starts_fresh_without_resume(
    tmp_path, monkeypatch
):
    """AC1+AC2: session owned by codex, dispatched for opencode → fresh start, no resume."""
    runner, mount_path, session_dir, _ = _make_session_with_transcript_owner(
        tmp_path,
        issue=2252,
        owner_service="codex",
        dispatch_service="opencode",
    )
    status_display = RecordingStatusDisplay()
    call_log: list[str] = []

    class _MismatchRuntimeClient:
        async def run_resumed_session(self, request):
            call_log.append("resumed")
            raise AssertionError(
                "run_resumed_session must not fire on service mismatch"
            )

        async def run_new_session(self, request):
            call_log.append("new")
            return RuntimeOutcome(
                kind=Completed(),
                result=RunResult(
                    output="<commit_message>done</commit_message>",
                    usage=None,
                    continuation=None,
                    selected=ResolvedProvider(
                        service="opencode",
                        model="gpt-5.5",
                        effort="medium",
                    ),
                ),
            )

    monkeypatch.setattr(
        runner, "_build_session", lambda *_args, **_kwargs: _FakeDockerSession()
    )
    monkeypatch.setattr(
        "pycastle.agents.runner.render_prompt_invocation",
        AsyncMock(return_value="prompt"),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner.setup",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner._get_runtime_client",
        lambda _self: _MismatchRuntimeClient(),
    )

    result = asyncio.run(
        runner.run(
            RunRequest(
                name="Implement Agent #2252",
                prompt=PromptInvocation(
                    template=PromptTemplate.IMPLEMENT_BEHAVIOR,
                    scope_args={
                        "ISSUE_NUMBER": "2252",
                        "ISSUE_TITLE": "Service switched from codex to opencode",
                        "ISSUE_BODY": "",
                        "ISSUE_COMMENTS": "",
                        "BRANCH": "issue-2252",
                        "INTERRUPTED_WORK": "",
                        "OPERATING_BRANCH": "main",
                    },
                ),
                mount_path=mount_path,
                role=AgentRole.IMPLEMENTER,
                model="gpt-5.5",
                effort="medium",
                service="opencode",
                status_display=status_display,
            )
        )
    )

    assert isinstance(result, CommitMessageOutput)
    assert call_log == ["new"], "must start fresh without a resume attempt"
    assert not (session_dir / "_continuation").is_file()


def test_same_service_transcript_owner_resumes_without_fresh_start(
    tmp_path, monkeypatch
):
    """AC3: session owned by codex, dispatched for codex → resumes normally."""
    runner, mount_path, _session_dir, _ = _make_session_with_transcript_owner(
        tmp_path,
        issue=2252,
        owner_service="codex",
        dispatch_service="codex",
    )
    status_display = RecordingStatusDisplay()
    call_log: list[str] = []

    class _SameServiceRuntimeClient:
        async def run_resumed_session(self, request):
            call_log.append("resumed")
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

        async def run_new_session(self, request):
            call_log.append("new")
            raise AssertionError(
                "run_new_session must not fire when service matches owner"
            )

    monkeypatch.setattr(
        runner, "_build_session", lambda *_args, **_kwargs: _FakeDockerSession()
    )
    monkeypatch.setattr(
        "pycastle.agents.runner.render_prompt_invocation",
        AsyncMock(return_value="prompt"),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner.setup",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner._get_runtime_client",
        lambda _self: _SameServiceRuntimeClient(),
    )

    result = asyncio.run(
        runner.run(
            RunRequest(
                name="Implement Agent #2252",
                prompt=PromptInvocation(
                    template=PromptTemplate.IMPLEMENT_BEHAVIOR,
                    scope_args={
                        "ISSUE_NUMBER": "2252",
                        "ISSUE_TITLE": "Service unchanged — still codex",
                        "ISSUE_BODY": "",
                        "ISSUE_COMMENTS": "",
                        "BRANCH": "issue-2252",
                        "INTERRUPTED_WORK": "",
                        "OPERATING_BRANCH": "main",
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
    assert call_log == ["resumed"], "must resume, not start fresh"


def test_no_transcript_owner_does_not_trigger_fresh_start(tmp_path, monkeypatch):
    """AC4: session with _continuation but no service subdir → resumes normally."""
    mount_path = tmp_path / "repo" / "pycastle" / ".worktrees" / "issue-2252-no-owner"
    mount_path.mkdir(parents=True)
    session_dir = mount_path / ".pycastle-session" / "implementer"
    session_dir.mkdir(parents=True)
    (session_dir / "_continuation").write_text(
        _VALID_STALE_CONTINUATION, encoding="utf-8"
    )

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
    call_log: list[str] = []

    class _OwnerlessRuntimeClient:
        async def run_resumed_session(self, request):
            call_log.append("resumed")
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

        async def run_new_session(self, request):
            call_log.append("new")
            raise AssertionError("no owner must not trigger a fresh start")

    monkeypatch.setattr(
        runner, "_build_session", lambda *_args, **_kwargs: _FakeDockerSession()
    )
    monkeypatch.setattr(
        "pycastle.agents.runner.render_prompt_invocation",
        AsyncMock(return_value="prompt"),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner.setup",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "pycastle.infrastructure.container_runner.ContainerRunner._get_runtime_client",
        lambda _self: _OwnerlessRuntimeClient(),
    )

    result = asyncio.run(
        runner.run(
            RunRequest(
                name="Implement Agent #2252",
                prompt=PromptInvocation(
                    template=PromptTemplate.IMPLEMENT_BEHAVIOR,
                    scope_args={
                        "ISSUE_NUMBER": "2252",
                        "ISSUE_TITLE": "No owner — should still resume",
                        "ISSUE_BODY": "",
                        "ISSUE_COMMENTS": "",
                        "BRANCH": "issue-2252-no-owner",
                        "INTERRUPTED_WORK": "",
                        "OPERATING_BRANCH": "main",
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
    assert call_log == ["resumed"], "no transcript owner must not trigger fresh start"
