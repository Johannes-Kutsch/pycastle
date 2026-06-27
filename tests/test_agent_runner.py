import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from agent_runtime.runtime import Completed, RunResult, RuntimeOutcome
from agent_runtime.types import ResolvedProvider

from pycastle.agents.output_protocol import AgentRole, CommitMessageOutput
from pycastle.agents.runner import AgentRunner, RunRequest
from pycastle.config import Config
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


class _FakeDockerSession:
    def __init__(self) -> None:
        self._container = type("Container", (), {"id": "container-123"})()

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


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
