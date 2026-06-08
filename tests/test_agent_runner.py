"""Tests for AgentRunner and FakeAgentRunner."""

import asyncio
import docker
import json
import threading
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from pycastle.agents.output_protocol import (
    AgentOutputProtocolError,
    AgentRole,
    CommitMessageOutput,
    CompletionOutput,
    FailedOutput,
    IssueOutput,
    PlannerOutput,
)
from pycastle.agents._work_invocation import (
    ProtocolOutputAdapter,
    TextOutputAdapter,
    WorkInvocationDependencies,
    WorkInvocationRequest,
    invoke_work,
)
from pycastle.agents.result import CancellationToken
from pycastle.agents.runner import AgentRunner, RunRequest
from pycastle.config import Config
from pycastle.errors import (
    AgentCredentialFailureError,
    AgentFailedError,
    AgentTimeoutError,
    DockerError,
    HardAgentError,
    SetupPhaseError,
    TransientAgentError,
    UsageLimitError,
)
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.session import ProviderRunState, RoleSession, RunKind
from pycastle.session.agent import RunSessionPlan
from pycastle.infrastructure.preflight_failure_interpreter import (
    PreflightCommandFailure,
)
from pycastle.infrastructure.container_runner import ContainerRunner
from pycastle.services.agent_service import ParsedTurn, Result
from pycastle.services import CodexService, GitCommandError, GitService, OpenCodeService
from pycastle.services.claude_service import ClaudeService
from pycastle.services.flag_profiles import AgentToolPolicyGroup
from pycastle.services.provider_session_state import (
    ProviderSessionState,
    ProviderSessionStateRequest,
)
from pycastle.display.status_display import ModelDisplayMetadata
from tests.support import FakeAgentRunner, RecordingStatusDisplay


@pytest.fixture(autouse=True)
def _project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


class _PreparedRunSessionStandIn:
    def __init__(
        self,
        *,
        initial_run_kind: RunKind,
        initial_provider_session_id: str | None,
        resumable_run_kind: RunKind | None = None,
        resumable_provider_session_id: str | None = None,
        provider_state_dir_container_path: str | None = None,
    ) -> None:
        self.provider_state_dir_container_path = provider_state_dir_container_path
        self.prepare_for_run_calls = 0
        self.initial_session = _PreparedProviderRunSessionStandIn(
            run_kind=initial_run_kind,
            provider_session_id=initial_provider_session_id,
        )
        self.resumable_session = _PreparedProviderRunSessionStandIn(
            run_kind=resumable_run_kind or initial_run_kind,
            provider_session_id=(
                initial_provider_session_id
                if resumable_provider_session_id is None
                else resumable_provider_session_id
            ),
        )

    def prepare_for_run(self) -> None:
        self.prepare_for_run_calls += 1

    def initial_provider_run_session(self) -> "_PreparedProviderRunSessionStandIn":
        return self.initial_session

    def resumable_provider_run_session(self) -> "_PreparedProviderRunSessionStandIn":
        return self.resumable_session

    def protocol_reprompt_provider_run_session(self):
        return None


class _PreparedRunSessionWithRepromptStandIn(_PreparedRunSessionStandIn):
    def __init__(
        self,
        *,
        initial_run_kind: RunKind,
        initial_provider_session_id: str | None,
        reprompt_run_kind: RunKind,
        reprompt_provider_session_id: str | None,
        provider_state_dir_container_path: str | None = None,
    ) -> None:
        super().__init__(
            initial_run_kind=initial_run_kind,
            initial_provider_session_id=initial_provider_session_id,
            provider_state_dir_container_path=provider_state_dir_container_path,
        )
        self.reprompt_session = _PreparedProviderRunSessionStandIn(
            run_kind=reprompt_run_kind,
            provider_session_id=reprompt_provider_session_id,
        )

    def protocol_reprompt_provider_run_session(
        self,
    ) -> "_PreparedProviderRunSessionStandIn":
        return self.reprompt_session


class _PreparedProviderRunSessionStandIn:
    def __init__(self, *, run_kind: RunKind, provider_session_id: str | None) -> None:
        self.run_kind = run_kind
        self.provider_session_id = provider_session_id
        self.recorded_provider_session_ids: list[str] = []
        self.successful_run_calls = 0

    def record_provider_session_id(self, provider_session_id: str) -> None:
        self.provider_session_id = provider_session_id
        self.recorded_provider_session_ids.append(provider_session_id)

    def record_successful_run(self) -> None:
        self.successful_run_calls += 1


def _make_cfg(tmp_path: Path, **kwargs) -> Config:
    """Create a Config with minimal project-local prompt overrides for AgentRunner tests."""
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "coordination").mkdir(exist_ok=True)
    (prompts_dir / "shared").mkdir(exist_ok=True)
    (prompts_dir / "coordination/plan.md").write_text(
        "{{ALL_OPEN_ISSUES_JSON}} {{READY_FOR_AGENT_ISSUES_JSON}}", encoding="utf-8"
    )
    (prompts_dir / "shared/resume.md").write_text("resume", encoding="utf-8")
    return Config(logs_dir=tmp_path, **kwargs)


def _run_request(*, service: str = "claude", **kwargs) -> RunRequest:
    return RunRequest(service=service, **kwargs)


_PLAN_TEMPLATE = PromptTemplate.PLAN
_PLAN_SCOPE_ARGS = {"ALL_OPEN_ISSUES_JSON": "[]", "READY_FOR_AGENT_ISSUES_JSON": "[]"}

# A minimal NDJSON stream that process_stream accepts as CommitMessageOutput (IMPLEMENTER role)
_COMPLETE_STREAM = [
    b'{"type": "result", "result": "<commit_message>done</commit_message>", "is_error": false}\n'
]

# A minimal NDJSON stream that process_stream accepts as CommitMessageOutput (REVIEWER role)
_REVIEWER_COMPLETE_STREAM = [
    b'{"type": "result", "result": "<commit_message>done</commit_message>", "is_error": false}\n'
]

# A minimal NDJSON stream that process_stream accepts as CommitMessageOutput (MERGER role)
_MERGER_COMPLETE_STREAM = [
    b'{"type": "result", "result": "<commit_message>done</commit_message>", "is_error": false}\n'
]

_DIVERGENCE_RESOLVER_FAILED_STREAM = [
    b'{"type": "result", "result": "<promise>FAILED</promise>", "is_error": false}\n'
]

_CODEX_COMPLETE_STREAM = [
    b'{"type":"thread.started","thread_id":"thread-from-fresh"}\n',
    b'{"type":"item.completed","item":{"type":"agent_message",'
    b'"content":"<commit_message>done</commit_message>"}}\n',
]


class _PlanRecordingClaudeService(ClaudeService):
    def __init__(self) -> None:
        super().__init__()
        self.fail_provider_session_state = False
        self.build_env_state_dir_args: list[str | None] = []

    def provider_session_state(
        self,
        request: ProviderSessionStateRequest,
    ) -> ProviderSessionState:
        if self.fail_provider_session_state:
            raise AssertionError("provider_session_state should not be recomputed")
        return super().provider_session_state(request)

    def build_env(
        self,
        state_dir_container_path: str | None = None,
        token: str | None = None,
    ) -> dict[str, str]:
        self.build_env_state_dir_args.append(state_dir_container_path)
        return super().build_env(
            state_dir_container_path=state_dir_container_path,
            token=token,
        )


# ── FakeAgentRunner: queue behaviour ─────────────────────────────────────────


def test_fake_agent_runner_returns_queued_completion_output():
    fake = FakeAgentRunner([CompletionOutput()])
    result = asyncio.run(
        fake.run(
            _run_request(
                name="Tester",
                template=_PLAN_TEMPLATE,
                mount_path=Path("/workspace"),
            )
        )
    )
    assert isinstance(result, CompletionOutput)


def test_fake_agent_runner_raises_queued_exception():
    fake = FakeAgentRunner([RuntimeError("boom")])
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(
            fake.run(
                _run_request(
                    name="Tester",
                    template=_PLAN_TEMPLATE,
                    mount_path=Path("/workspace"),
                )
            )
        )


def test_fake_agent_runner_raises_assertion_error_when_queue_exhausted():
    fake = FakeAgentRunner([])
    with pytest.raises(AssertionError, match="queue exhausted"):
        asyncio.run(
            fake.run(
                _run_request(
                    name="Unexpected",
                    template=_PLAN_TEMPLATE,
                    mount_path=Path("/workspace"),
                )
            )
        )


def test_fake_agent_runner_exhaustion_error_includes_agent_name():
    fake = FakeAgentRunner([])
    with pytest.raises(AssertionError, match="MyAgent"):
        asyncio.run(
            fake.run(
                _run_request(
                    name="MyAgent",
                    template=_PLAN_TEMPLATE,
                    mount_path=Path("/workspace"),
                )
            )
        )


def test_fake_agent_runner_pops_responses_in_order():
    r1, r2, r3 = CompletionOutput(), CompletionOutput(), CompletionOutput()
    fake = FakeAgentRunner([r1, r2, r3])
    run = fake.run

    async def _collect():
        m = Path("/w")
        return [
            await run(_run_request(name="A", template=_PLAN_TEMPLATE, mount_path=m)),
            await run(_run_request(name="B", template=_PLAN_TEMPLATE, mount_path=m)),
            await run(_run_request(name="C", template=_PLAN_TEMPLATE, mount_path=m)),
        ]

    results = asyncio.run(_collect())
    assert results == [r1, r2, r3]


def test_fake_agent_runner_records_all_calls():
    fake = FakeAgentRunner([CompletionOutput(), CompletionOutput()])
    mount = Path("/workspace")

    asyncio.run(
        fake.run(_run_request(name="X", template=_PLAN_TEMPLATE, mount_path=mount))
    )
    asyncio.run(
        fake.run(_run_request(name="Y", template=_PLAN_TEMPLATE, mount_path=mount))
    )

    assert len(fake.calls) == 2
    assert fake.calls[0].name == "X"
    assert fake.calls[1].name == "Y"


def test_fake_agent_runner_records_call_kwargs():
    fake = FakeAgentRunner([CompletionOutput()])
    mount = Path("/workspace")

    asyncio.run(
        fake.run(
            _run_request(
                name="Planner",
                template=PromptTemplate.PLAN,
                mount_path=mount,
                scope_args={
                    "ALL_OPEN_ISSUES_JSON": "[]",
                    "READY_FOR_AGENT_ISSUES_JSON": "[]",
                },
                model="claude-3",
                effort="high",
                stage="plan",
            )
        )
    )

    call = fake.calls[0]
    assert call.name == "Planner"
    assert call.template == PromptTemplate.PLAN
    assert call.mount_path == mount
    assert call.scope_args == {
        "ALL_OPEN_ISSUES_JSON": "[]",
        "READY_FOR_AGENT_ISSUES_JSON": "[]",
    }
    assert call.model == "claude-3"
    assert call.effort == "high"
    assert call.stage == "plan"


def test_fake_agent_runner_starts_with_empty_calls():
    fake = FakeAgentRunner([CompletionOutput()])
    assert fake.calls == []


# ── FakeAgentRunner: side_effect mode ────────────────────────────────────────


def test_fake_agent_runner_side_effect_is_called_with_run_request():
    received: dict = {}
    completion = CompletionOutput()

    async def _effect(request: RunRequest):
        received["name"] = request.name
        return completion

    fake = FakeAgentRunner(side_effect=_effect)
    result = asyncio.run(
        fake.run(
            _run_request(
                name="SideEffectAgent", template=_PLAN_TEMPLATE, mount_path=Path("/w")
            )
        )
    )

    assert result is completion
    assert received["name"] == "SideEffectAgent"


def test_fake_agent_runner_side_effect_can_raise():
    async def _effect(request: RunRequest):
        raise ValueError("side effect error")

    fake = FakeAgentRunner(side_effect=_effect)
    with pytest.raises(ValueError, match="side effect error"):
        asyncio.run(
            fake.run(
                _run_request(
                    name="Agent", template=_PLAN_TEMPLATE, mount_path=Path("/w")
                )
            )
        )


def test_fake_agent_runner_side_effect_still_records_calls():
    async def _effect(request: RunRequest):
        return CompletionOutput()

    fake = FakeAgentRunner(side_effect=_effect)
    asyncio.run(
        fake.run(
            _run_request(
                name="Recorded", template=_PLAN_TEMPLATE, mount_path=Path("/w")
            )
        )
    )

    assert len(fake.calls) == 1
    assert fake.calls[0].name == "Recorded"


def test_fake_agent_runner_side_effect_can_be_synchronous():
    completion = CompletionOutput()

    def _sync_effect(request: RunRequest):
        return completion

    fake = FakeAgentRunner(side_effect=_sync_effect)
    result = asyncio.run(
        fake.run(
            _run_request(name="Agent", template=_PLAN_TEMPLATE, mount_path=Path("/w"))
        )
    )

    assert result is completion


# ── AgentRunner: helpers ──────────────────────────────────────────────────────


def _make_docker_client(chunks: list[bytes]) -> MagicMock:
    """Mock docker client whose streaming exec_run replays the given byte chunks."""
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def exec_side_effect(*args, **kwargs):
        if kwargs.get("stream"):
            result = MagicMock()
            result.output = iter(chunks)
            return result
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect
    return mock_client


def _make_docker_client_with_setup_failure(message: str) -> MagicMock:
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def exec_side_effect(*args, **kwargs):
        if kwargs.get("stream"):
            result = MagicMock()
            result.output = iter(_COMPLETE_STREAM)
            return result
        command = " ".join(args[0]) if args else ""
        if "pip install" in command:
            return MagicMock(exit_code=1, output=(b"", message.encode("utf-8")))
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect
    return mock_client


def _make_docker_client_with_start_failure(message: str) -> MagicMock:
    mock_client = MagicMock()
    mock_client.containers.run.side_effect = docker.errors.APIError(message)
    return mock_client


def _make_docker_client_with_work_failure(message: str) -> MagicMock:
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def exec_side_effect(*args, **kwargs):
        if kwargs.get("stream"):
            raise DockerError(message)
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect
    return mock_client


def _make_git_service() -> MagicMock:
    svc = MagicMock(spec=GitService)
    svc.get_user_name.return_value = "Alice"
    svc.get_user_email.return_value = "alice@example.com"
    svc.is_working_tree_clean.return_value = True
    return svc


def _never_yields():
    """Generator that blocks forever without yielding — simulates a hung agent stream."""
    e = threading.Event()
    e.wait()
    yield  # make this a generator


class _RecordingAgentService:
    def __init__(
        self,
        name: str,
        *,
        state_dir_relpath: str | None = None,
        provider_run_state: ProviderRunState | None = None,
    ) -> None:
        self.name = name
        self._state_dir_relpath = state_dir_relpath
        self._provider_run_state = provider_run_state or ProviderRunState(
            RunKind.FRESH, None
        )
        self.commands: list[str] = []
        self.env_state_dirs: list[str | None] = []

    def build_command(
        self,
        role: AgentRole,
        model: str,
        effort: str,
        run_kind: RunKind,
        session_uuid: str | None,
        *,
        tool_policy=None,
    ) -> str:
        del role, model, effort, run_kind, session_uuid, tool_policy
        command = f"{self.name} exec"
        self.commands.append(command)
        return command

    def build_env(
        self,
        state_dir_container_path: str | None = None,
        token: str | None = None,
    ) -> dict[str, str]:
        self.env_state_dirs.append(state_dir_container_path)
        return {"PYCASTLE_TEST_SERVICE": self.name}

    def run(
        self,
        lines: Iterable[str],
        on_provider_session_id: Callable[[str], None] | None = None,
    ) -> Iterator[ParsedTurn]:
        list(lines)
        yield Result("<commit_message>done</commit_message>")

    def is_available(self, now: datetime | None = None) -> bool:
        return True

    def next_wake_time(self) -> datetime:
        return datetime.max

    def mark_exhausted(self, reset_time: datetime | None) -> None:
        pass

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        del role, namespace
        return self._state_dir_relpath

    def is_resumable(self, state_dir: Path) -> bool:
        return False

    def provider_session_state(
        self,
        request: ProviderSessionStateRequest,
    ) -> ProviderSessionState:
        del request
        return ProviderSessionState(
            self._provider_run_state.run_kind,
            self._provider_run_state.provider_session_id,
            persist_provider_session_id=self._provider_run_state.persist_provider_session_id,
        )

    def valid_efforts(self) -> frozenset[str]:
        return frozenset({"low", "medium", "high"})

    def valid_models(self) -> frozenset[str]:
        return frozenset({"test-model"})


# ── AgentRunner: run() return values ─────────────────────────────────────────


def test_agent_runner_run_returns_agent_output(tmp_path):
    mock_client = _make_docker_client(_COMPLETE_STREAM)
    runner = AgentRunner(
        {}, _make_cfg(tmp_path), _make_git_service(), docker_client=mock_client
    )

    result = asyncio.run(
        runner.run(
            _run_request(
                name="Test",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
            )
        )
    )

    assert isinstance(result, CommitMessageOutput)


def test_agent_runner_dispatches_with_explicit_claude_service(
    tmp_path,
):
    codex_service = _RecordingAgentService("codex")
    claude_service = _RecordingAgentService("claude")
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client([]),
        service_registry={"claude": claude_service, "codex": codex_service},
    )

    result = asyncio.run(
        runner.run(
            _run_request(
                name="Test",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
            )
        )
    )

    assert isinstance(result, CommitMessageOutput)
    assert claude_service.commands == ["claude exec"]
    assert codex_service.commands == []


def test_agent_runner_uses_requested_service_from_registry(tmp_path):
    claude_service = _RecordingAgentService("claude")
    requested_service = _RecordingAgentService("codex")
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client([]),
        service_registry={"claude": claude_service, "codex": requested_service},
    )

    result = asyncio.run(
        runner.run(
            _run_request(
                name="Test",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                service="codex",
            )
        )
    )

    assert isinstance(result, CommitMessageOutput)
    assert requested_service.commands == ["codex exec"]
    assert claude_service.commands == []


def test_agent_runner_uses_service_owned_provider_run_state_and_state_dir(
    tmp_path: Path,
):
    from unittest.mock import patch
    from pycastle.infrastructure.container_runner import ContainerRunner

    requested_service = _RecordingAgentService(
        "fake",
        state_dir_relpath=".pycastle-session/improve/fake/",
        provider_run_state=ProviderRunState(
            RunKind.RESUME,
            "provider-session-123",
        ),
    )
    work_calls: list[tuple[RunKind, str | None]] = []
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_setup_docker_client(),
        service_registry={"fake": requested_service},
    )

    async def _fake_work(
        _role, _prompt, *, run_kind, session_uuid, on_provider_session_id=None
    ):
        del on_provider_session_id
        work_calls.append((run_kind, session_uuid))
        return CommitMessageOutput(message="done")

    with patch.object(ContainerRunner, "work", side_effect=_fake_work):
        result = asyncio.run(
            runner.run(
                _run_request(
                    name="Improve",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    role=AgentRole.IMPROVE,
                    service="fake",
                    session_namespace="main",
                )
            )
        )

    assert isinstance(result, CommitMessageOutput)
    assert work_calls == [(RunKind.RESUME, "provider-session-123")]
    assert requested_service.env_state_dirs == [
        "/home/agent/workspace/.pycastle-session/improve/main/fake/"
    ]


def test_agent_runner_records_codex_provider_session_metadata_after_successful_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from unittest.mock import patch
    from pycastle.infrastructure.container_runner import ContainerRunner

    home = tmp_path / "home"
    host_auth = home / ".codex" / "auth.json"
    host_auth.parent.mkdir(parents=True)
    host_auth.write_text('{"mode":"oauth"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)

    async def _fake_work(
        _role, _prompt, *, run_kind, session_uuid, on_provider_session_id=None
    ):
        del run_kind, session_uuid
        assert on_provider_session_id is not None
        on_provider_session_id("thread-success")
        return CommitMessageOutput(message="done")

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_setup_docker_client(),
        service_registry={"codex": CodexService()},
    )

    with patch.object(ContainerRunner, "work", side_effect=_fake_work):
        result = asyncio.run(
            runner.run(
                _run_request(
                    name="Review",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    role=AgentRole.REVIEWER,
                    service="codex",
                )
            )
        )

    assert isinstance(result, CommitMessageOutput)
    assert RoleSession(tmp_path, AgentRole.REVIEWER).service_session_metadata(
        "codex"
    ) == {
        "service": "codex",
        "provider_session_id": "thread-success",
    }


def test_agent_runner_does_not_fall_back_to_claude_for_unknown_requested_service(
    tmp_path,
):
    claude_service = _RecordingAgentService("claude")
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client([]),
        service_registry={"claude": claude_service},
    )

    with pytest.raises(ValueError, match="Unknown agent service 'codex'"):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    service="codex",
                )
            )
        )

    assert claude_service.commands == []


@pytest.mark.parametrize("service_name", ["claude", "codex"])
def test_agent_runner_uses_universal_image_for_requested_service(
    tmp_path, service_name
):
    requested_service = _RecordingAgentService(service_name)
    docker_client = _make_docker_client([])
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path, docker_image_name="pycastle-test"),
        _make_git_service(),
        docker_client=docker_client,
        service_registry={service_name: requested_service},
    )

    result = asyncio.run(
        runner.run(
            _run_request(
                name="Test",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                service=service_name,
            )
        )
    )

    assert isinstance(result, CommitMessageOutput)
    docker_client.containers.run.assert_called_once()
    assert docker_client.containers.run.call_args.args[0] == "pycastle-test"


def test_agent_runner_requires_explicit_resolved_service_for_dispatch(tmp_path):
    cfg = _make_cfg(tmp_path, docker_image_name="pycastle-test")
    object.__setattr__(cfg, "default_service", "codex")
    codex_service = _RecordingAgentService("codex")
    claude_service = _RecordingAgentService("claude")
    docker_client = _make_docker_client([])
    runner = AgentRunner(
        {},
        cfg,
        _make_git_service(),
        docker_client=docker_client,
        service_registry={"claude": claude_service, "codex": codex_service},
    )

    with pytest.raises(ValueError, match="resolved service"):
        asyncio.run(
            runner.run(
                RunRequest(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )

    assert codex_service.commands == []
    assert codex_service.env_state_dirs == []
    assert claude_service.commands == []
    docker_client.containers.run.assert_not_called()


def test_agent_runner_requires_explicit_resolved_service_for_whitespace_only_service(
    tmp_path,
):
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client([]),
        service_registry={"claude": _RecordingAgentService("claude")},
    )

    with pytest.raises(ValueError, match="resolved service"):
        asyncio.run(
            runner.run(
                RunRequest(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    service="   ",
                )
            )
        )


def test_agent_runner_fails_when_no_explicit_service_even_if_default_service_is_empty(
    tmp_path,
):
    cfg = _make_cfg(tmp_path, docker_image_name="pycastle-test")
    object.__setattr__(cfg, "default_service", "")
    docker_client = _make_docker_client([])
    runner = AgentRunner(
        {},
        cfg,
        _make_git_service(),
        docker_client=docker_client,
        service_registry={"claude": _RecordingAgentService("claude")},
    )

    with pytest.raises(ValueError, match="resolved service"):
        asyncio.run(
            runner.run(
                RunRequest(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )

    docker_client.containers.run.assert_not_called()


# ── AgentRunner: error propagation ───────────────────────────────────────────


def test_agent_runner_run_raises_usage_limit_error_when_token_pre_cancelled(tmp_path):
    token = CancellationToken()
    token.cancel()
    mock_client = _make_docker_client([b"output\n"])
    runner = AgentRunner(
        {}, _make_cfg(tmp_path), _make_git_service(), docker_client=mock_client
    )

    with pytest.raises(UsageLimitError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    token=token,
                )
            )
        )

    mock_client.containers.run.assert_not_called()


def test_agent_runner_run_cancels_token_and_raises_on_usage_limit_in_stream(tmp_path):
    mock_client = _make_docker_client(
        [
            b'{"type":"result","is_error":true,"api_error_status":429,'
            b'"result":"rate limited"}\n'
        ]
    )
    token = CancellationToken()
    runner = AgentRunner(
        {}, _make_cfg(tmp_path), _make_git_service(), docker_client=mock_client
    )

    with pytest.raises(UsageLimitError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    token=token,
                )
            )
        )

    assert token.is_cancelled


def test_agent_runner_run_raises_agent_timeout_error_when_retries_exhausted(tmp_path):
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def exec_side_effect(*args, **kwargs):
        if kwargs.get("stream"):
            r = MagicMock()
            r.output = _never_yields()
            return r
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect
    cfg = _make_cfg(tmp_path, idle_timeout=0.01, timeout_retries=0)
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    with pytest.raises(AgentTimeoutError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )


def test_agent_runner_run_raises_agent_failed_error_for_non_typed_crash(tmp_path):
    from unittest.mock import AsyncMock, patch

    runner = AgentRunner({}, _make_cfg(tmp_path), _make_git_service())
    request = _run_request(
        name="Test",
        template=_PLAN_TEMPLATE,
        scope_args=_PLAN_SCOPE_ARGS,
        mount_path=tmp_path,
        role=AgentRole.IMPLEMENTER,
        session_namespace="test-ns",
    )

    with patch.object(
        runner,
        "_run",
        new=AsyncMock(return_value=FailedOutput(failure_class="non_typed_crash")),
    ):
        with pytest.raises(AgentFailedError) as exc_info:
            asyncio.run(runner.run(request))

    err = exc_info.value
    assert err.failure_class == "non_typed_crash"
    assert err.role_value == AgentRole.IMPLEMENTER.value
    assert err.worktree_path == tmp_path
    assert err.namespace == "test-ns"


def test_agent_runner_run_raises_agent_failed_error_for_protocol_error(tmp_path):
    from unittest.mock import AsyncMock, patch

    runner = AgentRunner({}, _make_cfg(tmp_path), _make_git_service())
    request = _run_request(
        name="Test",
        template=_PLAN_TEMPLATE,
        scope_args=_PLAN_SCOPE_ARGS,
        mount_path=tmp_path,
        role=AgentRole.PLANNER,
        session_namespace="",
    )

    with patch.object(
        runner,
        "_run",
        new=AsyncMock(return_value=FailedOutput(failure_class="protocol_error")),
    ):
        with pytest.raises(AgentFailedError) as exc_info:
            asyncio.run(runner.run(request))

    err = exc_info.value
    assert err.failure_class == "protocol_error"
    assert err.role_value == AgentRole.PLANNER.value
    assert err.worktree_path == tmp_path
    assert err.namespace == ""
    assert err.service_name == "claude"
    assert err.session_dir == ".pycastle-session/planner/claude"


def test_agent_runner_failed_output_reports_selected_service_session_dir(tmp_path):
    from unittest.mock import AsyncMock, patch

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        service_registry={"opencode": OpenCodeService()},
    )
    request = _run_request(
        name="Test",
        template=_PLAN_TEMPLATE,
        scope_args=_PLAN_SCOPE_ARGS,
        mount_path=tmp_path,
        role=AgentRole.PLANNER,
        service="opencode",
    )

    with patch.object(
        runner,
        "_run",
        new=AsyncMock(return_value=FailedOutput(failure_class="protocol_error")),
    ):
        with pytest.raises(AgentFailedError) as exc_info:
            asyncio.run(runner.run(request))

    err = exc_info.value
    assert err.service_name == "opencode"
    assert err.session_dir == ".pycastle-session/planner/opencode"


@pytest.mark.parametrize(
    ("role", "template", "scope_args"),
    [
        (AgentRole.PLANNER, _PLAN_TEMPLATE, _PLAN_SCOPE_ARGS),
        (
            AgentRole.PREFLIGHT_ISSUE,
            PromptTemplate.PREFLIGHT_ISSUE,
            {"CHECK_NAME": "ruff", "COMMAND": "ruff check .", "OUTPUT": "missing"},
        ),
    ],
)
def test_agent_runner_run_raises_setup_phase_error_when_setup_fails_before_work(
    tmp_path,
    role,
    template,
    scope_args,
):
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client_with_setup_failure("pip install failed"),
    )
    request = _run_request(
        name="Role Agent",
        template=template,
        scope_args=scope_args,
        mount_path=tmp_path,
        role=role,
    )

    with pytest.raises(SetupPhaseError) as exc_info:
        asyncio.run(runner.run(request))

    assert exc_info.value.phase == role.value
    assert "pip install failed" in str(exc_info.value)


def test_agent_runner_run_raises_setup_phase_error_when_container_start_fails(
    tmp_path,
):
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path, docker_image_name="pycastle-test"),
        _make_git_service(),
        docker_client=_make_docker_client_with_start_failure(
            "exec: sleep: executable file not found in $PATH"
        ),
    )

    with pytest.raises(SetupPhaseError) as exc_info:
        asyncio.run(
            runner.run(
                _run_request(
                    name="Host-Check Reporter",
                    template=PromptTemplate.PREFLIGHT_ISSUE,
                    scope_args={
                        "CHECK_NAME": "[PREFLIGHT] reporter",
                        "COMMAND": "pytest",
                        "OUTPUT": "docker start failed",
                    },
                    mount_path=tmp_path,
                    role=AgentRole.PREFLIGHT_ISSUE,
                )
            )
        )

    assert exc_info.value.phase == AgentRole.PREFLIGHT_ISSUE.value
    assert "pycastle-test" in str(exc_info.value)
    assert "sleep: executable file not found in $PATH" in str(exc_info.value)


def test_agent_runner_run_propagates_work_failures_after_setup_starts(tmp_path):
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client_with_work_failure("stream broke"),
    )

    with pytest.raises(DockerError, match="stream broke"):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Plan Agent",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    role=AgentRole.PLANNER,
                )
            )
        )


def test_agent_runner_run_retries_on_timeout_and_returns_output(tmp_path):
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    stream_call_count = {"n": 0}

    def exec_side_effect(*args, **kwargs):
        if kwargs.get("stream"):
            stream_call_count["n"] += 1
            r = MagicMock()
            r.output = (
                _never_yields()
                if stream_call_count["n"] == 1
                else iter(_COMPLETE_STREAM)
            )
            return r
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect
    cfg = _make_cfg(tmp_path, idle_timeout=0.01, timeout_retries=1)
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(
        runner.run(
            _run_request(
                name="Test",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
            )
        )
    )

    assert isinstance(result, CommitMessageOutput)


def test_agent_runner_propagates_git_user_name_error(tmp_path):
    mock_git = _make_git_service()
    mock_git.get_user_name.side_effect = GitCommandError("git config user.name failed")
    mock_client = _make_docker_client(_COMPLETE_STREAM)
    runner = AgentRunner({}, _make_cfg(tmp_path), mock_git, docker_client=mock_client)

    with pytest.raises(GitCommandError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )


# ── AgentRunner: status row lifecycle ────────────────────────────────────────


def test_agent_runner_run_registers_and_removes_status_row_on_success(tmp_path):
    mock_client = _make_docker_client(_COMPLETE_STREAM)
    runner = AgentRunner(
        {}, _make_cfg(tmp_path), _make_git_service(), docker_client=mock_client
    )
    display = RecordingStatusDisplay()

    asyncio.run(
        runner.run(
            _run_request(
                name="Test",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                model="sonnet",
                effort="medium",
                status_display=display,
            )
        )
    )

    assert (
        "register",
        "Test",
        "agent",
        "started",
        "Setup",
        ModelDisplayMetadata(service="claude", model="sonnet", effort="medium"),
    ) in display.calls
    assert ("remove", "Test", "finished", "success") in display.calls


def test_agent_runner_run_removes_status_row_when_setup_fails(tmp_path):
    git_svc = _make_git_service()
    git_svc.get_user_name.side_effect = RuntimeError("git failure")
    runner = AgentRunner({}, _make_cfg(tmp_path), git_svc, docker_client=MagicMock())
    display = RecordingStatusDisplay()

    with pytest.raises(RuntimeError, match="git failure"):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    model="sonnet",
                    effort="medium",
                    status_display=display,
                )
            )
        )

    assert (
        "register",
        "Test",
        "agent",
        "started",
        "Setup",
        ModelDisplayMetadata(service="claude", model="sonnet", effort="medium"),
    ) in display.calls
    assert ("remove", "Test", "failed", "error") in display.calls


def test_agent_runner_run_marks_failed_output_as_failed_in_status_row(tmp_path):
    mock_client = _make_docker_client(_DIVERGENCE_RESOLVER_FAILED_STREAM)
    runner = AgentRunner(
        {}, _make_cfg(tmp_path), _make_git_service(), docker_client=mock_client
    )
    display = RecordingStatusDisplay()

    with pytest.raises(AgentFailedError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    role=AgentRole.DIVERGENCE_RESOLVER,
                    model="sonnet",
                    effort="medium",
                    status_display=display,
                )
            )
        )

    assert (
        "register",
        "Test",
        "agent",
        "started",
        "Setup",
        ModelDisplayMetadata(service="claude", model="sonnet", effort="medium"),
    ) in display.calls
    assert ("remove", "Test", "failed", "error") in display.calls


# ── AgentRunner: run_preflight ────────────────────────────────────────────────


def _make_preflight_docker_client(exit_code: int = 0, stdout: bytes = b"") -> MagicMock:
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def _exec_run(cmd, **kwargs):
        command_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "git config" in command_str or "pip install" in command_str:
            return MagicMock(exit_code=0, output=(b"", b""))
        return MagicMock(exit_code=exit_code, output=(stdout, b""))

    mock_container.exec_run.side_effect = _exec_run
    return mock_client


def test_agent_runner_run_preflight_returns_empty_list_when_no_checks_configured(
    tmp_path,
):
    mock_client = _make_preflight_docker_client()
    cfg = _make_cfg(tmp_path, preflight_checks=())
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert result == []


def test_agent_runner_run_preflight_does_not_require_resolved_service(tmp_path):
    mock_client = _make_preflight_docker_client(exit_code=0)
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    object.__setattr__(cfg, "default_service", "claude")
    runner = AgentRunner(
        {},
        cfg,
        _make_git_service(),
        docker_client=mock_client,
        service_registry={"codex": CodexService()},
    )

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert result == []


def test_agent_runner_run_preflight_returns_empty_list_when_all_checks_pass(tmp_path):
    mock_client = _make_preflight_docker_client(exit_code=0)
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert result == []


def test_agent_runner_run_preflight_raises_setup_phase_error_when_setup_fails(
    tmp_path,
):
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client_with_setup_failure("pip install failed"),
    )

    with pytest.raises(SetupPhaseError) as exc_info:
        asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert exc_info.value.phase == "preflight"
    assert "pip install failed" in str(exc_info.value)


def test_agent_runner_run_preflight_raises_setup_phase_error_when_container_start_fails(
    tmp_path,
):
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path, docker_image_name="pycastle-test"),
        _make_git_service(),
        docker_client=_make_docker_client_with_start_failure(
            "exec: sleep: executable file not found in $PATH"
        ),
    )

    with pytest.raises(SetupPhaseError) as exc_info:
        asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert exc_info.value.phase == "preflight"
    assert "pycastle-test" in str(exc_info.value)
    assert "sleep: executable file not found in $PATH" in str(exc_info.value)


def test_agent_runner_run_preflight_returns_typed_failure_when_check_fails(tmp_path):
    mock_client = _make_preflight_docker_client(
        exit_code=1, stdout=b"E501 line too long"
    )
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert len(result) == 1
    assert result[0].check_name == "ruff"
    assert result[0].command == "ruff check ."
    assert "E501" in result[0].output


def test_agent_runner_run_preflight_collects_all_failures_when_multiple_checks_fail(
    tmp_path,
):
    mock_client = _make_preflight_docker_client(exit_code=1, stdout=b"check failed")
    cfg = Config(
        logs_dir=tmp_path,
        preflight_checks=(("ruff", "ruff check ."), ("mypy", "mypy .")),
    )
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert len(result) == 2
    assert result[0].check_name == "ruff"
    assert result[1].check_name == "mypy"


def test_agent_runner_run_preflight_stops_container_after_checks_pass(tmp_path):
    mock_client = _make_preflight_docker_client()
    cfg = _make_cfg(tmp_path)
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    mock_client.containers.run.return_value.stop.assert_called()


def test_agent_runner_run_preflight_stops_container_when_check_fails(tmp_path):
    mock_client = _make_preflight_docker_client(exit_code=1, stdout=b"check failed")
    cfg = _make_cfg(tmp_path, preflight_checks=(("lint", "lint ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    mock_client.containers.run.return_value.stop.assert_called()


def test_agent_runner_run_preflight_raises_setup_phase_error_when_pip_install_fails(
    tmp_path,
):
    # If pip install fails during Setup, the preflight container must abort via
    # the shared setup-failure path rather than continuing with ruff absent.
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def _exec_run(cmd, **kwargs):
        command_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "git config" in command_str:
            return MagicMock(exit_code=0, output=(b"", b""))
        if "pip install" in command_str:
            return MagicMock(
                exit_code=1, output=(b"", b"ERROR: Could not find a version")
            )
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = _exec_run
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    with pytest.raises(SetupPhaseError) as exc_info:
        asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert exc_info.value.phase == "preflight"


def test_agent_runner_run_preflight_returns_failure_tuple_for_missing_pyproject_declared_tool(
    tmp_path,
):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 't'\ndependencies = ['ruff>=0.5']\n", encoding="utf-8"
    )
    mock_client = _make_preflight_docker_client(
        exit_code=127, stdout=b"bash: ruff: command not found"
    )
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert result == [
        PreflightCommandFailure(
            check_name="ruff",
            command="ruff check .",
            output="Command failed (exit 127): bash: ruff: command not found",
        )
    ]


def test_agent_runner_run_preflight_returns_failure_tuple_for_missing_requirements_declared_tool(
    tmp_path,
):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 't'\ndependencies = ['click>=8']\n", encoding="utf-8"
    )
    (tmp_path / "requirements.txt").write_text("ruff==0.6.9\n", encoding="utf-8")
    mock_client = _make_preflight_docker_client(
        exit_code=127, stdout=b"bash: ruff: command not found"
    )
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert result == [
        PreflightCommandFailure(
            check_name="ruff",
            command="ruff check .",
            output="Command failed (exit 127): bash: ruff: command not found",
        )
    ]


def test_agent_runner_run_preflight_ignores_malformed_pyproject_and_returns_raw_failure(
    tmp_path,
):
    (tmp_path / "pyproject.toml").write_text("[project\nname = 't'\n", encoding="utf-8")
    mock_client = _make_preflight_docker_client(
        exit_code=127, stdout=b"bash: ruff: command not found"
    )
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert result == [
        PreflightCommandFailure(
            check_name="ruff",
            command="ruff check .",
            output="Command failed (exit 127): bash: ruff: command not found",
        )
    ]


def test_agent_runner_run_preflight_returns_failure_tuple_for_missing_undeclared_tool(
    tmp_path,
):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 't'\ndependencies = ['ruff>=0.5']\n", encoding="utf-8"
    )
    mock_client = _make_preflight_docker_client(
        exit_code=127, stdout=b"bash: black: command not found"
    )
    cfg = _make_cfg(tmp_path, preflight_checks=(("black", "black --check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert result == [
        PreflightCommandFailure(
            check_name="black",
            command="black --check .",
            output="Command failed (exit 127): bash: black: command not found",
        )
    ]


def test_agent_runner_run_preflight_keeps_missing_tool_without_python_declaration_as_ordinary_failure(
    tmp_path,
):
    (tmp_path / "requirements.txt").write_text("pytest==9.0.0\n", encoding="utf-8")
    mock_client = _make_preflight_docker_client(
        exit_code=127, stdout=b"bash: shellcheck: command not found"
    )
    cfg = _make_cfg(tmp_path, preflight_checks=(("shellcheck", "shellcheck ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert result == [
        PreflightCommandFailure(
            check_name="shellcheck",
            command="shellcheck .",
            output="Command failed (exit 127): bash: shellcheck: command not found",
        )
    ]


def test_agent_runner_run_preflight_returns_failure_tuple_for_declared_tool_project_failure(
    tmp_path,
):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 't'\ndependencies = ['ruff>=0.5']\n", encoding="utf-8"
    )
    mock_client = _make_preflight_docker_client(
        exit_code=1, stdout=b"src/app.py:1:1: F401 imported but unused"
    )
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert result == [
        PreflightCommandFailure(
            check_name="ruff",
            command="ruff check .",
            output="Command failed (exit 1): src/app.py:1:1: F401 imported but unused",
        )
    ]


def test_agent_runner_run_preflight_returns_all_failures_after_running_later_checks(
    tmp_path,
):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 't'\ndependencies = ['ruff>=0.5']\n", encoding="utf-8"
    )
    exec_calls: list[str] = []
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def _exec_run(cmd, **kwargs):
        command_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        exec_calls.append(command_str)
        if "git config" in command_str or "pip install" in command_str:
            return MagicMock(exit_code=0, output=(b"", b""))
        if "ruff check ." in command_str:
            return MagicMock(
                exit_code=127, output=(b"bash: ruff: command not found", b"")
            )
        if "mypy ." in command_str:
            return MagicMock(exit_code=1, output=(b"src/app.py:1: error: boom", b""))
        raise AssertionError(f"unexpected command: {command_str}")

    mock_container.exec_run.side_effect = _exec_run
    cfg = _make_cfg(
        tmp_path,
        preflight_checks=(("ruff", "ruff check ."), ("mypy", "mypy .")),
    )
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    ruff_idx = next(i for i, call in enumerate(exec_calls) if "ruff check ." in call)
    mypy_idx = next(i for i, call in enumerate(exec_calls) if "mypy ." in call)
    assert ruff_idx < mypy_idx
    assert result == [
        PreflightCommandFailure(
            check_name="ruff",
            command="ruff check .",
            output="Command failed (exit 127): bash: ruff: command not found",
        ),
        PreflightCommandFailure(
            check_name="mypy",
            command="mypy .",
            output="Command failed (exit 1): src/app.py:1: error: boom",
        ),
    ]


def test_agent_runner_run_preflight_returns_all_ordinary_failures_in_configured_order(
    tmp_path,
):
    exec_calls: list[str] = []
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def _exec_run(cmd, **kwargs):
        command_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        exec_calls.append(command_str)
        if "git config" in command_str or "pip install" in command_str:
            return MagicMock(exit_code=0, output=(b"", b""))
        if "ruff check ." in command_str:
            return MagicMock(exit_code=1, output=(b"src/app.py:1:1: F401", b""))
        if "mypy ." in command_str:
            return MagicMock(exit_code=0, output=(b"", b""))
        if "pytest" in command_str:
            return MagicMock(exit_code=1, output=(b"FAILED tests/test_app.py", b""))
        raise AssertionError(f"unexpected command: {command_str}")

    mock_container.exec_run.side_effect = _exec_run
    cfg = _make_cfg(
        tmp_path,
        preflight_checks=(
            ("ruff", "ruff check ."),
            ("mypy", "mypy ."),
            ("pytest", "pytest"),
        ),
    )
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert result == [
        PreflightCommandFailure(
            check_name="ruff",
            command="ruff check .",
            output="Command failed (exit 1): src/app.py:1:1: F401",
        ),
        PreflightCommandFailure(
            check_name="pytest",
            command="pytest",
            output="Command failed (exit 1): FAILED tests/test_app.py",
        ),
    ]
    assert any("mypy ." in call for call in exec_calls)


def test_agent_runner_run_preflight_passes_checks_that_require_installed_tools(
    tmp_path,
):
    # Simulates the original bug: ruff fails with exit 127 (command not found)
    # if the Setup phase hasn't run pip install first. Verifies the fix: setup
    # runs before preflight so the check succeeds.
    setup_done = {"value": False}
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def _exec_run(cmd, **kwargs):
        command_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "git config" in command_str:
            return MagicMock(exit_code=0, output=(b"", b""))
        if "pip install" in command_str:
            setup_done["value"] = True
            return MagicMock(exit_code=0, output=(b"", b""))
        if "ruff check" in command_str:
            if not setup_done["value"]:
                return MagicMock(
                    exit_code=127, output=(b"bash: ruff: command not found", b"")
                )
            return MagicMock(exit_code=0, output=(b"", b""))
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = _exec_run
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert result == []


def test_agent_runner_run_preflight_preserves_agent_user_console_script_path(
    tmp_path,
):
    setup_installed_console_script = {"value": False}
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def _exec_run(cmd, **kwargs):
        command_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "git config" in command_str:
            return MagicMock(exit_code=0, output=(b"", b""))
        if "pip install" in command_str:
            setup_installed_console_script["value"] = True
            return MagicMock(exit_code=0, output=(b"", b""))
        if "demo-tool --version" in command_str:
            if not setup_installed_console_script["value"]:
                return MagicMock(
                    exit_code=127, output=(b"bash: demo-tool: command not found", b"")
                )
            if 'export PATH="/home/agent/.local/bin:$PATH";' not in command_str:
                return MagicMock(
                    exit_code=127, output=(b"bash: demo-tool: command not found", b"")
                )
            return MagicMock(exit_code=0, output=(b"demo-tool 1.0.0", b""))
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = _exec_run
    cfg = _make_cfg(tmp_path, preflight_checks=(("demo-tool", "demo-tool --version"),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert result == []


# ── AgentRunner: run_preflight status_display ────────────────────────────────


def test_agent_runner_run_preflight_registers_and_removes_status_row_on_success(
    tmp_path,
):
    mock_client = _make_preflight_docker_client()
    cfg = _make_cfg(tmp_path, preflight_checks=())
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)
    display = RecordingStatusDisplay()

    asyncio.run(
        runner.run_preflight(
            name="preflight-checks", mount_path=tmp_path, status_display=display
        )
    )

    assert (
        "register",
        "preflight-checks",
        "agent",
        "started",
        "Setup",
        None,
    ) in display.calls
    assert (
        "remove",
        "preflight-checks",
        "finished, all tests green",
        "success",
    ) in display.calls


def test_agent_runner_run_preflight_updates_phase_for_each_check(tmp_path):
    mock_client = _make_preflight_docker_client()
    checks = (("ruff", "ruff check ."), ("mypy", "mypy ."), ("pytest", "pytest"))
    cfg = _make_cfg(tmp_path, preflight_checks=checks)
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)
    display = RecordingStatusDisplay()

    asyncio.run(
        runner.run_preflight(
            name="preflight-checks", mount_path=tmp_path, status_display=display
        )
    )

    phase_updates = [c for c in display.calls if c[0] == "update_phase"]
    assert any(c[2] == "Running ruff (1/3)" for c in phase_updates)
    assert any(c[2] == "Running mypy (2/3)" for c in phase_updates)
    assert any(c[2] == "Running pytest (3/3)" for c in phase_updates)


def test_agent_runner_run_preflight_removes_status_row_when_checks_fail(tmp_path):
    mock_client = _make_preflight_docker_client(exit_code=1, stdout=b"E501")
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)
    display = RecordingStatusDisplay()

    asyncio.run(
        runner.run_preflight(
            name="preflight-checks", mount_path=tmp_path, status_display=display
        )
    )

    assert ("remove", "preflight-checks", "finished", "success") in display.calls


def test_agent_runner_run_preflight_removes_status_row_when_exception_propagates(
    tmp_path,
):
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def _exec_run(cmd, **kwargs):
        command_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "git config" in command_str or "pip install" in command_str:
            return MagicMock(exit_code=0, output=(b"", b""))
        raise RuntimeError("unexpected container error")

    mock_container.exec_run.side_effect = _exec_run
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)
    display = RecordingStatusDisplay()

    with pytest.raises(RuntimeError, match="unexpected container error"):
        asyncio.run(
            runner.run_preflight(
                name="preflight-checks", mount_path=tmp_path, status_display=display
            )
        )

    assert ("remove", "preflight-checks", "failed", "error") in display.calls


def test_agent_runner_run_preflight_renders_all_tests_green_when_checks_pass(tmp_path):
    mock_client = _make_preflight_docker_client(exit_code=0)
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)
    display = RecordingStatusDisplay()

    asyncio.run(
        runner.run_preflight(
            name="preflight-checks", mount_path=tmp_path, status_display=display
        )
    )

    assert (
        "remove",
        "preflight-checks",
        "finished, all tests green",
        "success",
    ) in display.calls


def test_agent_runner_run_preflight_propagates_git_user_name_error(tmp_path):
    mock_git = _make_git_service()
    mock_git.get_user_name.side_effect = GitCommandError("git config user.name failed")
    runner = AgentRunner({}, _make_cfg(tmp_path), mock_git, docker_client=MagicMock())

    with pytest.raises(GitCommandError):
        asyncio.run(runner.run_preflight(name="preflight-checks", mount_path=tmp_path))


# ── RunRequest: core interface ────────────────────────────────────────────────


def test_run_request_stores_required_fields():
    from pycastle.agents.output_protocol import AgentRole

    req = _run_request(
        name="Agent",
        template=PromptTemplate.PLAN,
        mount_path=Path("/workspace"),
    )
    assert req.name == "Agent"
    assert req.template == PromptTemplate.PLAN
    assert req.mount_path == Path("/workspace")
    assert req.role == AgentRole.IMPLEMENTER
    assert req.scope_args is None
    assert req.model == ""
    assert req.effort == ""
    assert req.stage == ""
    assert req.token is None
    assert req.status_display is None
    assert req.issue_title == ""
    assert req.work_body == ""
    assert req.session_namespace == ""


def test_run_request_session_namespace_can_be_set():
    req = _run_request(
        name="Agent",
        template=PromptTemplate.PLAN,
        mount_path=Path("/workspace"),
        session_namespace="main",
    )
    assert req.session_namespace == "main"


# ── AgentRunner: ClaudeService pool integration ───────────────────────────────


def test_agent_runner_injects_picked_token_into_container_env(tmp_path):
    from pycastle.services.claude_service import ClaudeService

    captured_env: dict = {}
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def _run(*args, **kwargs):
        captured_env.update(kwargs.get("environment") or {})
        return mock_container

    mock_client.containers.run.side_effect = _run

    def exec_side_effect(*args, **kwargs):
        if kwargs.get("stream"):
            r = MagicMock()
            r.output = iter(_COMPLETE_STREAM)
            return r
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect

    svc = ClaudeService(
        accounts=[("secondary", "tok-secondary"), ("primary", "tok-primary")]
    )
    runner = AgentRunner(
        {"GH_TOKEN": "gh"},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
        service_registry={"claude": svc},
    )

    asyncio.run(
        runner.run(
            _run_request(
                name="Test",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
            )
        )
    )

    assert captured_env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-secondary"


def test_agent_runner_filters_host_env_before_container_startup(tmp_path):
    started_envs: list[dict[str, str]] = []
    mock_client = MagicMock()
    mock_container = MagicMock()

    def _start_container(*args, **kwargs):
        started_envs.append(dict(kwargs.get("environment") or {}))
        return mock_container

    def _exec_run(*args, **kwargs):
        if kwargs.get("stream"):
            result = MagicMock()
            result.output = iter(_COMPLETE_STREAM)
            return result
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_client.containers.run.side_effect = _start_container
    mock_container.exec_run.side_effect = _exec_run

    runner = AgentRunner(
        {
            "GH_TOKEN": "gh-token",
            "PATH": r"C:\Windows\System32;C:\Windows",
            "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY": "secondary-token",
        },
        _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),)),
        _make_git_service(),
        docker_client=mock_client,
        service_registry={
            "claude": ClaudeService(accounts=[("primary", "tok-primary")])
        },
    )

    asyncio.run(
        runner.run(
            _run_request(
                name="Implement",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                role=AgentRole.IMPLEMENTER,
                service="claude",
            )
        )
    )
    asyncio.run(runner.run_preflight(name="preflight", mount_path=tmp_path))

    agent_env, preflight_env = started_envs
    assert agent_env["GH_TOKEN"] == "gh-token"
    assert "PATH" not in agent_env
    assert "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY" not in agent_env
    assert agent_env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-primary"
    assert "CLAUDE_CONFIG_DIR" in agent_env

    assert preflight_env["GH_TOKEN"] == "gh-token"
    assert "PATH" not in preflight_env
    assert "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY" not in preflight_env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in preflight_env


def test_agent_runner_keeps_service_env_across_preflight_issue_review_and_failure_report(
    tmp_path,
    monkeypatch,
):
    issue_stream = [
        b'{"type": "result", "result": "<issue>{\\"number\\": 123, \\"labels\\": [\\"bug\\"]}</issue>", "is_error": false}\n'
    ]
    opencode_issue_stream = [
        b'{"type":"text","sessionID":"sess-from-fresh",'
        b'"part":{"type":"text","text":"<issue>{\\"number\\": 456, \\"labels\\": [\\"bug\\"]}</issue>",'
        b'"time":{"start":1,"end":2}}}\n',
        b'{"type":"session.status","sessionID":"sess-from-fresh",'
        b'"status":{"type":"idle"}}\n',
    ]
    started: list[tuple[str, dict[str, str]]] = []
    mock_client = MagicMock()
    mock_container = MagicMock()
    home = tmp_path / "home"
    host_auth = home / ".codex" / "auth.json"
    host_auth.parent.mkdir(parents=True)
    host_auth.write_text('{"mode":"oauth"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)

    def _start_container(image_name: str, **kwargs):
        started.append((image_name, dict(kwargs.get("environment") or {})))
        return mock_container

    def _exec_run(cmd, **kwargs):
        if not kwargs.get("stream"):
            return MagicMock(exit_code=0, output=(b"", b""))
        command = cmd[2] if isinstance(cmd, list) and len(cmd) > 2 else ""
        result = MagicMock()
        if "opencode run" in command:
            result.output = iter(opencode_issue_stream)
        elif "codex exec" in command:
            result.output = iter(_CODEX_COMPLETE_STREAM)
        else:
            result.output = iter(issue_stream)
        return result

    mock_client.containers.run.side_effect = _start_container
    mock_container.exec_run.side_effect = _exec_run

    runner = AgentRunner(
        {
            "GH_TOKEN": "gh-token",
            "PATH": r"C:\Windows\System32;C:\Windows",
            "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY": "secondary-token",
            "OPENCODE_GO_API_KEY": "opencode-key",
        },
        _make_cfg(tmp_path, docker_image_name="pycastle-test"),
        _make_git_service(),
        docker_client=mock_client,
        service_registry={
            "claude": ClaudeService(accounts=[("primary", "tok-primary")]),
            "codex": CodexService(),
            "opencode": OpenCodeService(api_key="opencode-key"),
        },
    )

    preflight_issue = asyncio.run(
        runner.run(
            _run_request(
                name="Host-Check Reporter",
                template=PromptTemplate.HOST_CHECK_ISSUE,
                scope_args={
                    "HOST_OS": "Windows",
                    "HOST_PLATFORM": "Windows-11",
                    "CHECKED_SHA": "abc123",
                    "CHECK_NAME": "pytest",
                    "COMMAND": "pytest tests/host",
                    "OUTPUT": "failed",
                },
                mount_path=tmp_path,
                role=AgentRole.PREFLIGHT_ISSUE,
                service="opencode",
            )
        )
    )
    review = asyncio.run(
        runner.run(
            _run_request(
                name="Review",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                role=AgentRole.REVIEWER,
                service="codex",
            )
        )
    )
    failure_report = asyncio.run(
        runner.run(
            _run_request(
                name="Failure Report",
                template=PromptTemplate.FAILURE_REPORT,
                scope_args={
                    "SESSION_DIR": "/tmp/session",
                    "FAILED_ROLE": "reviewer",
                    "FAILURE_CLASS": "protocol_error",
                },
                mount_path=tmp_path,
                role=AgentRole.FAILURE_REPORT,
                service="claude",
            )
        )
    )

    assert isinstance(preflight_issue, IssueOutput)
    assert isinstance(review, CommitMessageOutput)
    assert isinstance(failure_report, IssueOutput)

    opencode_env = started[0][1]
    codex_env = started[1][1]
    claude_env = started[2][1]

    assert started[0][0] == "pycastle-test"
    assert opencode_env["GH_TOKEN"] == "gh-token"
    assert opencode_env["OPENCODE_GO_API_KEY"] == "opencode-key"
    assert "OPENCODE_CONFIG_CONTENT" in opencode_env
    assert opencode_env["OPENCODE_HOME"].endswith(
        "/.pycastle-session/preflight_issue/opencode/"
    )
    assert "PATH" not in opencode_env
    assert "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY" not in opencode_env

    assert started[1][0] == "pycastle-test"
    assert codex_env["GH_TOKEN"] == "gh-token"
    assert codex_env["TZ"] == "UTC"
    assert codex_env["CODEX_HOME"].endswith("/.pycastle-session/reviewer/codex/")
    assert "PATH" not in codex_env
    assert "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY" not in codex_env

    assert started[2][0] == "pycastle-test"
    assert claude_env["GH_TOKEN"] == "gh-token"
    assert claude_env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-primary"
    assert claude_env["CLAUDE_CONFIG_DIR"].endswith(
        "/.pycastle-session/failure_report/claude/"
    )
    assert "PATH" not in claude_env
    assert "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY" not in claude_env


def test_agent_runner_cancels_token_and_raises_on_transient_agent_error(tmp_path):
    """TransientAgentError from a 5xx result cancels the CancellationToken and re-raises."""
    mock_client = _make_docker_client(
        [
            b'{"type":"result","is_error":true,"api_error_status":529,'
            b'"result":"API Error: 529 Overloaded"}\n'
        ]
    )
    token = CancellationToken()
    runner = AgentRunner(
        {}, _make_cfg(tmp_path), _make_git_service(), docker_client=mock_client
    )

    with pytest.raises(TransientAgentError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Implement Agent #42",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    token=token,
                )
            )
        )

    assert token.is_cancelled


def test_agent_runner_does_not_call_mark_exhausted_on_transient_agent_error(tmp_path):
    """TransientAgentError must NOT mark the account exhausted (server-wide, not account-specific)."""
    from pycastle.services.claude_service import ClaudeService

    mock_client = _make_docker_client(
        [
            b'{"type":"result","is_error":true,"api_error_status":529,'
            b'"result":"API Error: 529 Overloaded"}\n'
        ]
    )
    svc = ClaudeService(accounts=[("primary", "tok-primary")])
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
        service_registry={"claude": svc},
    )

    with pytest.raises(TransientAgentError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )

    # Account must still be available — mark_exhausted was NOT called
    assert svc.is_available() is True


def test_agent_runner_marks_picked_token_exhausted_on_usage_limit(tmp_path):
    from pycastle.services.claude_service import ClaudeService

    mock_client = _make_docker_client(
        [
            b'{"type":"result","is_error":true,"api_error_status":429,'
            b'"result":"rate limited"}\n'
        ]
    )

    svc = ClaudeService(
        accounts=[("secondary", "tok-secondary"), ("primary", "tok-primary")]
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
        service_registry={"claude": svc},
    )

    with pytest.raises(UsageLimitError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )

    # secondary was picked (highest priority) and marked exhausted; primary should now be available
    assert svc.is_available() is True
    env = svc.build_env()
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-primary"


def test_agent_runner_routes_subscription_access_denial_to_agent_credential_failure(
    tmp_path,
):
    from pycastle.services.claude_service import ClaudeService

    denial = (
        "Your organization has disabled Claude subscription access for Claude Code. "
        "Please use an Anthropic API key instead, or ask your admin to enable "
        "Claude subscription access for Claude Code."
    )
    mock_client = _make_docker_client(
        [
            (
                b'{"type":"result","is_error":true,"api_error_status":403,'
                b'"result":"' + denial.encode() + b'"}\n'
            )
        ]
    )

    svc = ClaudeService(
        accounts=[("secondary", "tok-secondary"), ("primary", "tok-primary")]
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
        service_registry={"claude": svc},
    )

    with pytest.raises(AgentCredentialFailureError) as exc_info:
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )

    assert exc_info.value.service_name == "claude"
    assert exc_info.value.status_code == 403
    env = svc.build_env()
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-secondary"


def test_agent_runner_routes_subscription_access_denial_variant_to_agent_credential_failure(
    tmp_path,
):
    from pycastle.services.claude_service import ClaudeService

    denial = (
        "Your organization has disabled Claude subscription access for Claude Code\n"
        "· Use an Anthropic API key instead, or ask your admin to enable Claude "
        "subscription access for Claude Code."
    )
    mock_client = _make_docker_client(
        [
            json.dumps(
                {
                    "type": "result",
                    "is_error": True,
                    "api_error_status": 403,
                    "result": denial,
                }
            ).encode()
            + b"\n"
        ]
    )

    svc = ClaudeService(
        accounts=[("secondary", "tok-secondary"), ("primary", "tok-primary")]
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
        service_registry={"claude": svc},
    )

    with pytest.raises(AgentCredentialFailureError) as exc_info:
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )

    assert exc_info.value.service_name == "claude"
    assert exc_info.value.status_code == 403
    env = svc.build_env()
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-secondary"


def test_agent_runner_treats_unrelated_403_as_hard_error(tmp_path):
    mock_client = _make_docker_client(
        [
            json.dumps(
                {
                    "type": "result",
                    "is_error": True,
                    "api_error_status": 403,
                    "result": "Forbidden: your IP address is not allowed.",
                }
            ).encode()
            + b"\n"
        ]
    )
    runner = AgentRunner(
        {}, _make_cfg(tmp_path), _make_git_service(), docker_client=mock_client
    )

    with pytest.raises(HardAgentError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )


def test_agent_runner_codex_missing_host_auth_fails_before_container_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    docker_client = _make_docker_client([])
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=docker_client,
        service_registry={"codex": CodexService()},
    )

    with pytest.raises(HardAgentError) as exc_info:
        asyncio.run(
            runner.run(
                _run_request(
                    name="Codex",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    role=AgentRole.PLANNER,
                    service="codex",
                )
            )
        )

    assert exc_info.value.status_code == 401
    docker_client.containers.run.assert_not_called()


def test_agent_runner_codex_resume_with_provider_auth_keeps_existing_auth_without_host_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from unittest.mock import patch
    from pycastle.infrastructure.container_runner import ContainerRunner

    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    state_dir = tmp_path / ".pycastle-session" / "reviewer" / "codex"
    state_dir.mkdir(parents=True)
    auth_path = state_dir / "auth.json"
    auth_path.write_text(
        '{"mode":"oauth","origin":"provider"}',
        encoding="utf-8",
    )
    RoleSession(tmp_path, AgentRole.REVIEWER).save_service_session_id(
        "codex", "thread-existing"
    )
    work_calls: list[tuple[RunKind, str | None]] = []

    async def _fake_work(
        _role, _prompt, *, run_kind, session_uuid, on_provider_session_id=None
    ):
        del on_provider_session_id
        work_calls.append((run_kind, session_uuid))
        return CommitMessageOutput(message="done")

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_setup_docker_client(),
        service_registry={"codex": CodexService()},
    )

    with patch.object(ContainerRunner, "work", side_effect=_fake_work):
        result = asyncio.run(
            runner.run(
                _run_request(
                    name="Review",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    role=AgentRole.REVIEWER,
                    service="codex",
                )
            )
        )

    assert isinstance(result, CommitMessageOutput)
    assert work_calls == [(RunKind.RESUME, "thread-existing")]
    assert auth_path.read_text(encoding="utf-8") == (
        '{"mode":"oauth","origin":"provider"}'
    )


def test_agent_runner_claude_resumes_with_derived_role_session_uuid_from_local_state(
    tmp_path: Path,
):
    from unittest.mock import patch
    from pycastle.infrastructure.container_runner import ContainerRunner

    _seed_claude_resumable_state(
        tmp_path,
        role=AgentRole.IMPROVE,
        namespace="main",
    )
    work_calls: list[tuple[RunKind, str | None]] = []
    expected_session_id = RoleSession(
        tmp_path,
        AgentRole.IMPROVE,
        "main",
    ).session_uuid()

    async def _fake_work(
        _role, _prompt, *, run_kind, session_uuid, on_provider_session_id=None
    ):
        del on_provider_session_id
        work_calls.append((run_kind, session_uuid))
        return CommitMessageOutput(message="done")

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_setup_docker_client(),
        service_registry={"claude": ClaudeService()},
    )

    with patch.object(ContainerRunner, "work", side_effect=_fake_work):
        result = asyncio.run(
            runner.run(
                _run_request(
                    name="Improve",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    role=AgentRole.IMPROVE,
                    service="claude",
                    session_namespace="main",
                )
            )
        )

    assert isinstance(result, CommitMessageOutput)
    assert work_calls == [(RunKind.RESUME, expected_session_id)]


def test_agent_runner_claude_fresh_dispatch_uses_supplied_run_session_plan(
    tmp_path: Path,
):
    from unittest.mock import patch
    from pycastle.infrastructure.container_runner import ContainerRunner

    service = _PlanRecordingClaudeService()
    plan = RunSessionPlan.for_service(
        role=AgentRole.PLANNER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )
    service.fail_provider_session_state = True
    work_calls: list[tuple[RunKind, str | None, str]] = []

    async def _fake_work(
        _role, prompt, *, run_kind, session_uuid, on_provider_session_id=None
    ):
        del on_provider_session_id
        work_calls.append((run_kind, session_uuid, prompt))
        return PlannerOutput(issues=[])

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_setup_docker_client(),
        service_registry={"claude": service},
    )

    with patch.object(ContainerRunner, "work", side_effect=_fake_work):
        result = asyncio.run(
            runner.run(
                _run_request(
                    name="Planner",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    role=AgentRole.PLANNER,
                    service="claude",
                    run_session_plan=plan,
                )
            )
        )

    assert isinstance(result, PlannerOutput)
    assert work_calls == [(RunKind.FRESH, plan.provider_session_id, "[] []")]
    assert service.build_env_state_dir_args == [
        "/home/agent/workspace/.pycastle-session/planner/claude/"
    ]


def test_agent_runner_claude_resume_dispatch_uses_supplied_run_session_plan(
    tmp_path: Path,
):
    from unittest.mock import patch
    from pycastle.infrastructure.container_runner import ContainerRunner

    state_dir = tmp_path / ".pycastle-session" / "planner" / "claude"
    state_dir.mkdir(parents=True)
    (state_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")

    service = _PlanRecordingClaudeService()
    plan = RunSessionPlan.for_service(
        role=AgentRole.PLANNER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )
    service.fail_provider_session_state = True
    work_calls: list[tuple[RunKind, str | None, str]] = []

    async def _fake_work(
        _role, prompt, *, run_kind, session_uuid, on_provider_session_id=None
    ):
        del on_provider_session_id
        work_calls.append((run_kind, session_uuid, prompt))
        return PlannerOutput(issues=[])

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_setup_docker_client(),
        service_registry={"claude": service},
    )

    with patch.object(ContainerRunner, "work", side_effect=_fake_work):
        result = asyncio.run(
            runner.run(
                _run_request(
                    name="Planner",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    role=AgentRole.PLANNER,
                    service="claude",
                    run_session_plan=plan,
                )
            )
        )

    assert isinstance(result, PlannerOutput)
    assert work_calls == [(RunKind.RESUME, plan.provider_session_id, "resume")]
    assert service.build_env_state_dir_args == [
        "/home/agent/workspace/.pycastle-session/planner/claude/"
    ]


def test_agent_runner_claude_resume_dispatch_with_role_prompt_flag_uses_role_prompt_from_supplied_run_session_plan(
    tmp_path: Path,
):
    from unittest.mock import patch
    from pycastle.infrastructure.container_runner import ContainerRunner

    state_dir = tmp_path / ".pycastle-session" / "planner" / "claude"
    state_dir.mkdir(parents=True)
    (state_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")

    service = _PlanRecordingClaudeService()
    plan = RunSessionPlan.for_service(
        role=AgentRole.PLANNER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )
    service.fail_provider_session_state = True
    work_calls: list[tuple[RunKind, str | None, str]] = []

    async def _fake_work(
        _role, prompt, *, run_kind, session_uuid, on_provider_session_id=None
    ):
        del on_provider_session_id
        work_calls.append((run_kind, session_uuid, prompt))
        return PlannerOutput(issues=[])

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_setup_docker_client(),
        service_registry={"claude": service},
    )

    with patch.object(ContainerRunner, "work", side_effect=_fake_work):
        result = asyncio.run(
            runner.run(
                _run_request(
                    name="Planner",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    role=AgentRole.PLANNER,
                    service="claude",
                    send_role_prompt_on_resume=True,
                    run_session_plan=plan,
                )
            )
        )

    assert isinstance(result, PlannerOutput)
    assert work_calls == [(RunKind.RESUME, plan.provider_session_id, "[] []")]


def test_agent_runner_codex_resumes_with_recovered_thread_id_from_local_rollout_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from unittest.mock import patch
    from pycastle.infrastructure.container_runner import ContainerRunner

    home = tmp_path / "home"
    host_auth = home / ".codex" / "auth.json"
    host_auth.parent.mkdir(parents=True)
    host_auth.write_text('{"mode":"oauth"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    _seed_codex_rollout_state(
        tmp_path,
        role=AgentRole.REVIEWER,
        thread_id="thread-from-rollout",
    )
    work_calls: list[tuple[RunKind, str | None]] = []

    async def _fake_work(
        _role, _prompt, *, run_kind, session_uuid, on_provider_session_id=None
    ):
        del on_provider_session_id
        work_calls.append((run_kind, session_uuid))
        return CommitMessageOutput(message="done")

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_setup_docker_client(),
        service_registry={"codex": CodexService()},
    )

    with patch.object(ContainerRunner, "work", side_effect=_fake_work):
        result = asyncio.run(
            runner.run(
                _run_request(
                    name="Review",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    role=AgentRole.REVIEWER,
                    service="codex",
                )
            )
        )

    assert isinstance(result, CommitMessageOutput)
    assert work_calls == [(RunKind.RESUME, "thread-from-rollout")]


def test_agent_runner_codex_starts_fresh_without_provider_session_id_when_local_thread_identity_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from unittest.mock import patch
    from pycastle.infrastructure.container_runner import ContainerRunner

    home = tmp_path / "home"
    host_auth = home / ".codex" / "auth.json"
    host_auth.parent.mkdir(parents=True)
    host_auth.write_text('{"mode":"oauth"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    _seed_ambiguous_codex_rollout_state(tmp_path, role=AgentRole.REVIEWER)
    work_calls: list[tuple[RunKind, str | None]] = []

    async def _fake_work(
        _role, _prompt, *, run_kind, session_uuid, on_provider_session_id=None
    ):
        del on_provider_session_id
        work_calls.append((run_kind, session_uuid))
        return CommitMessageOutput(message="done")

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_setup_docker_client(),
        service_registry={"codex": CodexService()},
    )

    with patch.object(ContainerRunner, "work", side_effect=_fake_work):
        result = asyncio.run(
            runner.run(
                _run_request(
                    name="Review",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    role=AgentRole.REVIEWER,
                    service="codex",
                )
            )
        )

    assert isinstance(result, CommitMessageOutput)
    assert work_calls == [(RunKind.FRESH, None)]


@pytest.mark.parametrize(
    ("session_id", "expected_call"),
    [
        ("sess-opencode-123", (RunKind.RESUME, "sess-opencode-123")),
        ("", (RunKind.FRESH, None)),
        (None, (RunKind.FRESH, None)),
    ],
)
def test_agent_runner_opencode_chooses_resume_only_when_local_session_id_sidecar_exists(
    tmp_path: Path,
    session_id: str | None,
    expected_call: tuple[RunKind, str | None],
):
    from unittest.mock import patch
    from pycastle.infrastructure.container_runner import ContainerRunner

    _seed_opencode_state(
        tmp_path,
        role=AgentRole.IMPROVE,
        namespace="main",
        session_id=session_id,
    )
    work_calls: list[tuple[RunKind, str | None]] = []

    async def _fake_work(
        _role, _prompt, *, run_kind, session_uuid, on_provider_session_id=None
    ):
        del on_provider_session_id
        work_calls.append((run_kind, session_uuid))
        return CommitMessageOutput(message="done")

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_setup_docker_client(),
        service_registry={"opencode": OpenCodeService()},
    )

    with patch.object(ContainerRunner, "work", side_effect=_fake_work):
        result = asyncio.run(
            runner.run(
                _run_request(
                    name="Improve",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    role=AgentRole.IMPROVE,
                    service="opencode",
                    session_namespace="main",
                )
            )
        )

    assert isinstance(result, CommitMessageOutput)
    assert work_calls == [expected_call]


def test_fake_agent_runner_accepts_run_request_and_records_it():
    completion = CompletionOutput()
    fake = FakeAgentRunner([completion])
    req = _run_request(
        name="Planner",
        template=_PLAN_TEMPLATE,
        mount_path=Path("/w"),
    )
    result = asyncio.run(fake.run(req))
    assert result is completion
    assert fake.calls[0] is req


def _seed_implementer_session(tmp_path: Path) -> None:
    """Seed the claude service state dir so ClaudeService.is_resumable returns True."""
    claude_dir = tmp_path / ".pycastle-session" / "implementer" / "claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "session.json").write_text("{}")


def _seed_claude_resumable_state(
    tmp_path: Path,
    *,
    role: AgentRole,
    namespace: str = "",
) -> None:
    claude_dir = RoleSession(tmp_path, role, namespace).path / "claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "session.json").write_text("{}")


def _seed_codex_rollout_state(
    tmp_path: Path,
    *,
    role: AgentRole,
    thread_id: str,
    namespace: str = "",
) -> None:
    codex_dir = RoleSession(tmp_path, role, namespace).path / "codex"
    rollout_dir = codex_dir / "sessions" / "2026" / "05" / "31"
    rollout_dir.mkdir(parents=True)
    (codex_dir / "auth.json").write_text('{"mode":"oauth"}', encoding="utf-8")
    (rollout_dir / "rollout-001.jsonl").write_text(
        json.dumps({"type": "thread.started", "thread_id": thread_id}) + "\n",
        encoding="utf-8",
    )


def _seed_ambiguous_codex_rollout_state(
    tmp_path: Path,
    *,
    role: AgentRole,
    namespace: str = "",
) -> None:
    codex_dir = RoleSession(tmp_path, role, namespace).path / "codex"
    rollout_dir = codex_dir / "sessions" / "2026" / "05" / "31"
    rollout_dir.mkdir(parents=True)
    (codex_dir / "auth.json").write_text('{"mode":"oauth"}', encoding="utf-8")
    (rollout_dir / "rollout-001.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-a"}),
                json.dumps({"type": "thread.started", "thread_id": "thread-b"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _seed_opencode_state(
    tmp_path: Path,
    *,
    role: AgentRole,
    session_id: str | None,
    namespace: str = "",
) -> None:
    opencode_dir = RoleSession(tmp_path, role, namespace).path / "opencode"
    opencode_dir.mkdir(parents=True)
    (opencode_dir / "resume.jsonl").write_text("{}\n", encoding="utf-8")
    if session_id is not None:
        (opencode_dir / "session_id").write_text(f"{session_id}\n", encoding="utf-8")


# ── AgentRunner: non-typed Resume retry ───────────────────────────────────────


def _make_docker_client_with_controlled_streams(
    stream_responses: list,
) -> MagicMock:
    """Mock docker client whose nth streaming exec_run returns or raises stream_responses[n]."""
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container
    responses = iter(stream_responses)

    def exec_side_effect(*args, **kwargs):
        if kwargs.get("stream"):
            response = next(responses, RuntimeError("unexpected call"))
            if isinstance(response, BaseException):
                raise response
            r = MagicMock()
            r.output = iter(response)
            return r
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect
    return mock_client


def test_resume_run_non_typed_exception_retries_same_session_and_succeeds(tmp_path):
    """On a Resume run, a non-typed exception triggers one in-call retry; success on retry returns output."""
    _seed_implementer_session(tmp_path)

    mock_client = _make_docker_client_with_controlled_streams(
        [RuntimeError("transient error"), _COMPLETE_STREAM]
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
    )

    result = asyncio.run(
        runner.run(
            _run_request(
                name="Impl",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
            )
        )
    )

    assert isinstance(result, CommitMessageOutput)


def test_resume_run_consecutive_non_typed_exceptions_raise_agent_failed_error(tmp_path):
    """On a Resume run, two consecutive non-typed exceptions cause AgentRunner.run to raise AgentFailedError."""
    _seed_implementer_session(tmp_path)

    mock_client = _make_docker_client_with_controlled_streams(
        [RuntimeError("first failure"), RuntimeError("second failure")]
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
    )

    with pytest.raises(AgentFailedError) as exc_info:
        asyncio.run(
            runner.run(
                _run_request(
                    name="Impl",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )

    assert exc_info.value.failure_class == "non_typed_crash"


def test_work_invocation_resume_non_typed_retry_raises_agent_failed_error(
    tmp_path: Path,
):
    _seed_implementer_session(tmp_path)
    status_display = RecordingStatusDisplay()
    work_calls: list[tuple[RunKind, str | None, str]] = []

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> CommitMessageOutput:
            del role, on_provider_session_id
            work_calls.append((run_kind, session_uuid, prompt))
            raise RuntimeError(f"boom-{len(work_calls)}")

    service = ClaudeService()
    session = _FakeSession()
    runner = _FakeRunner()
    expected_session_id = RoleSession(
        tmp_path,
        AgentRole.IMPLEMENTER,
        "",
    ).session_uuid()

    async def prompt_factory(*, run_kind: RunKind, container_exec) -> str:
        del run_kind, container_exec
        return "resume"

    with pytest.raises(AgentFailedError) as exc_info:
        asyncio.run(
            invoke_work(
                WorkInvocationRequest(
                    name="Impl",
                    mount_path=tmp_path,
                    role=AgentRole.IMPLEMENTER,
                    service=service,
                    model="sonnet",
                    effort="high",
                    output_adapter=ProtocolOutputAdapter(
                        prompt_factory=prompt_factory,
                        reprompt_message="reprompt",
                    ),
                    dependencies=WorkInvocationDependencies(
                        container_workspace="/home/agent/workspace",
                        timeout_retries=0,
                        stage_key_for_role=lambda role: role.value,
                        build_session=lambda *_args: session,
                        build_runner=lambda *_args: cast(ContainerRunner, runner),
                        get_git_identity=lambda: ("Test User", "test@example.com"),
                    ),
                    status_display=status_display,
                    allow_non_typed_resume_retry=True,
                )
            )
        )

    assert exc_info.value.failure_class == "non_typed_crash"
    assert exc_info.value.service_name == "claude"
    assert work_calls == [
        (RunKind.RESUME, expected_session_id, "resume"),
        (RunKind.RESUME, expected_session_id, "resume"),
    ]
    assert ("remove", "Impl", "failed", "error") in status_display.calls


def test_work_invocation_typed_fresh_success_returns_adapter_output_and_forwarded_prompt(
    tmp_path: Path,
):
    status_display = RecordingStatusDisplay()
    prepared_session = _PreparedRunSessionStandIn(
        initial_run_kind=RunKind.FRESH,
        initial_provider_session_id="provider-fresh",
        provider_state_dir_container_path="/workspace/provider-state",
    )
    session_state_dirs: list[str | None] = []
    work_calls: list[tuple[RunKind, str | None, str]] = []

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> PlannerOutput:
            del role, on_provider_session_id
            work_calls.append((run_kind, session_uuid, prompt))
            return PlannerOutput(issues=[])

    service = ClaudeService()
    session = _FakeSession()
    runner = _FakeRunner()

    def build_session(
        _mount_path: Path, _service, state_dir: str | None
    ) -> _FakeSession:
        session_state_dirs.append(state_dir)
        return session

    async def prompt_factory(*, run_kind: RunKind, container_exec) -> str:
        del container_exec
        assert run_kind is RunKind.FRESH
        return "caller-rendered prompt text"

    result = asyncio.run(
        invoke_work(
            WorkInvocationRequest(
                name="Planner",
                mount_path=tmp_path,
                role=AgentRole.PLANNER,
                service=service,
                model="sonnet",
                effort="high",
                output_adapter=ProtocolOutputAdapter(
                    prompt_factory=prompt_factory,
                    reprompt_message="reprompt",
                ),
                dependencies=WorkInvocationDependencies(
                    container_workspace="/home/agent/workspace",
                    timeout_retries=0,
                    stage_key_for_role=lambda role: role.value,
                    prepare_session=lambda _request: prepared_session,
                    build_session=build_session,
                    build_runner=lambda *_args: cast(ContainerRunner, runner),
                    get_git_identity=lambda: ("Test User", "test@example.com"),
                ),
                status_display=status_display,
            )
        )
    )

    assert result == PlannerOutput(issues=[])
    assert work_calls == [
        (RunKind.FRESH, "provider-fresh", "caller-rendered prompt text")
    ]
    assert session_state_dirs == ["/workspace/provider-state"]
    assert prepared_session.prepare_for_run_calls == 1
    assert prepared_session.initial_session.successful_run_calls == 1


def test_work_invocation_typed_resume_success_uses_prepared_run_kind_and_provider_session_id(
    tmp_path: Path,
):
    prepared_session = _PreparedRunSessionStandIn(
        initial_run_kind=RunKind.RESUME,
        initial_provider_session_id="provider-resume",
    )
    work_calls: list[tuple[RunKind, str | None, str]] = []

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> PlannerOutput:
            del role, on_provider_session_id
            work_calls.append((run_kind, session_uuid, prompt))
            return PlannerOutput(issues=[])

    async def prompt_factory(*, run_kind: RunKind, container_exec) -> str:
        del container_exec
        assert run_kind is RunKind.RESUME
        return "resume prompt from adapter"

    result = asyncio.run(
        invoke_work(
            WorkInvocationRequest(
                name="Planner",
                mount_path=tmp_path,
                role=AgentRole.PLANNER,
                service=ClaudeService(),
                model="sonnet",
                effort="high",
                output_adapter=ProtocolOutputAdapter(
                    prompt_factory=prompt_factory,
                    reprompt_message="reprompt",
                ),
                dependencies=WorkInvocationDependencies(
                    container_workspace="/home/agent/workspace",
                    timeout_retries=0,
                    stage_key_for_role=lambda role: role.value,
                    prepare_session=lambda _request: prepared_session,
                    build_session=lambda *_args: _FakeSession(),
                    build_runner=lambda *_args: cast(ContainerRunner, _FakeRunner()),
                    get_git_identity=lambda: ("Test User", "test@example.com"),
                ),
            )
        )
    )

    assert result == PlannerOutput(issues=[])
    assert work_calls == [
        (RunKind.RESUME, "provider-resume", "resume prompt from adapter")
    ]
    assert prepared_session.initial_session.successful_run_calls == 1


@pytest.mark.parametrize(
    ("initial_run_kind", "initial_provider_session_id", "observed_provider_session_id"),
    [
        (RunKind.FRESH, "provider-fresh", "provider-fresh-runtime"),
        (RunKind.RESUME, "provider-resume", "provider-resume-runtime"),
    ],
)
def test_work_invocation_text_success_returns_exact_str_and_records_provider_session_metadata(
    tmp_path: Path,
    initial_run_kind: RunKind,
    initial_provider_session_id: str,
    observed_provider_session_id: str,
):
    prepared_session = _PreparedRunSessionStandIn(
        initial_run_kind=initial_run_kind,
        initial_provider_session_id=initial_provider_session_id,
    )
    work_calls: list[
        tuple[str, AgentRole, AgentToolPolicyGroup, RunKind, str | None]
    ] = []

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work_text(
            self,
            prompt: str,
            *,
            role: AgentRole = AgentRole.IMPLEMENTER,
            tool_policy: AgentToolPolicyGroup = AgentToolPolicyGroup.FULL,
            run_kind: RunKind = RunKind.FRESH,
            session_uuid: str | None = None,
            on_provider_session_id=None,
        ) -> str:
            work_calls.append((prompt, role, tool_policy, run_kind, session_uuid))
            assert on_provider_session_id is not None
            on_provider_session_id(observed_provider_session_id)
            return "exact text from adapter"

    result = asyncio.run(
        invoke_work(
            WorkInvocationRequest(
                name="Runtime Consumer",
                mount_path=tmp_path,
                role=AgentRole.IMPLEMENTER,
                service=ClaudeService(),
                model="sonnet",
                effort="high",
                output_adapter=TextOutputAdapter(
                    prompt="already-rendered prompt text",
                    tool_policy=AgentToolPolicyGroup.PARTIAL,
                ),
                dependencies=WorkInvocationDependencies(
                    container_workspace="/home/agent/workspace",
                    timeout_retries=0,
                    stage_key_for_role=lambda role: role.value,
                    prepare_session=lambda _request: prepared_session,
                    build_session=lambda *_args: _FakeSession(),
                    build_runner=lambda *_args: cast(ContainerRunner, _FakeRunner()),
                    get_git_identity=lambda: ("Test User", "test@example.com"),
                ),
            )
        )
    )

    assert result == "exact text from adapter"
    assert work_calls == [
        (
            "already-rendered prompt text",
            AgentRole.IMPLEMENTER,
            AgentToolPolicyGroup.PARTIAL,
            initial_run_kind,
            initial_provider_session_id,
        )
    ]
    assert prepared_session.initial_session.recorded_provider_session_ids == [
        observed_provider_session_id
    ]
    assert prepared_session.initial_session.provider_session_id == (
        observed_provider_session_id
    )
    assert prepared_session.initial_session.successful_run_calls == 1


@pytest.mark.parametrize(
    ("initial_run_kind", "initial_provider_session_id", "observed_provider_session_id"),
    [
        (RunKind.FRESH, "provider-fresh", "provider-fresh-runtime"),
        (RunKind.RESUME, "provider-resume", "provider-resume-runtime"),
    ],
)
def test_work_invocation_typed_success_records_provider_session_metadata_through_prepared_run_session(
    tmp_path: Path,
    initial_run_kind: RunKind,
    initial_provider_session_id: str,
    observed_provider_session_id: str,
):
    prepared_session = _PreparedRunSessionStandIn(
        initial_run_kind=initial_run_kind,
        initial_provider_session_id=initial_provider_session_id,
    )

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> PlannerOutput:
            del role, prompt, run_kind, session_uuid
            assert on_provider_session_id is not None
            on_provider_session_id(observed_provider_session_id)
            return PlannerOutput(issues=[])

    async def prompt_factory(*, run_kind: RunKind, container_exec) -> str:
        del run_kind, container_exec
        return "caller-rendered prompt text"

    asyncio.run(
        invoke_work(
            WorkInvocationRequest(
                name="Planner",
                mount_path=tmp_path,
                role=AgentRole.PLANNER,
                service=ClaudeService(),
                model="sonnet",
                effort="high",
                output_adapter=ProtocolOutputAdapter(
                    prompt_factory=prompt_factory,
                    reprompt_message="reprompt",
                ),
                dependencies=WorkInvocationDependencies(
                    container_workspace="/home/agent/workspace",
                    timeout_retries=0,
                    stage_key_for_role=lambda role: role.value,
                    prepare_session=lambda _request: prepared_session,
                    build_session=lambda *_args: _FakeSession(),
                    build_runner=lambda *_args: cast(ContainerRunner, _FakeRunner()),
                    get_git_identity=lambda: ("Test User", "test@example.com"),
                ),
            )
        )
    )

    assert prepared_session.initial_session.recorded_provider_session_ids == [
        observed_provider_session_id
    ]
    assert prepared_session.initial_session.provider_session_id == (
        observed_provider_session_id
    )
    assert prepared_session.initial_session.successful_run_calls == 1


def test_work_invocation_protocol_reprompt_success_records_metadata_on_successful_reprompt_run(
    tmp_path: Path,
):
    prepared_session = _PreparedRunSessionWithRepromptStandIn(
        initial_run_kind=RunKind.FRESH,
        initial_provider_session_id="provider-fresh",
        reprompt_run_kind=RunKind.RESUME,
        reprompt_provider_session_id="provider-resume",
    )
    work_calls: list[tuple[RunKind, str | None, str]] = []

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> PlannerOutput:
            del role
            work_calls.append((run_kind, session_uuid, prompt))
            if len(work_calls) == 1:
                raise AgentOutputProtocolError("missing tag")
            assert on_provider_session_id is not None
            on_provider_session_id("provider-resume-runtime")
            return PlannerOutput(issues=[])

    async def prompt_factory(*, run_kind: RunKind, container_exec) -> str:
        del container_exec
        assert run_kind is RunKind.FRESH
        return "caller-rendered prompt text"

    result = asyncio.run(
        invoke_work(
            WorkInvocationRequest(
                name="Planner",
                mount_path=tmp_path,
                role=AgentRole.PLANNER,
                service=ClaudeService(),
                model="sonnet",
                effort="high",
                output_adapter=ProtocolOutputAdapter(
                    prompt_factory=prompt_factory,
                    reprompt_message="reprompt",
                ),
                dependencies=WorkInvocationDependencies(
                    container_workspace="/home/agent/workspace",
                    timeout_retries=0,
                    stage_key_for_role=lambda role: role.value,
                    prepare_session=lambda _request: prepared_session,
                    build_session=lambda *_args: _FakeSession(),
                    build_runner=lambda *_args: cast(ContainerRunner, _FakeRunner()),
                    get_git_identity=lambda: ("Test User", "test@example.com"),
                ),
            )
        )
    )

    assert result == PlannerOutput(issues=[])
    assert work_calls == [
        (RunKind.FRESH, "provider-fresh", "caller-rendered prompt text"),
        (RunKind.RESUME, "provider-resume", "reprompt"),
    ]
    assert prepared_session.initial_session.successful_run_calls == 0
    assert prepared_session.reprompt_session.recorded_provider_session_ids == [
        "provider-resume-runtime"
    ]
    assert prepared_session.reprompt_session.provider_session_id == (
        "provider-resume-runtime"
    )
    assert prepared_session.reprompt_session.successful_run_calls == 1


def test_work_invocation_protocol_reprompt_exhaustion_raises_protocol_error_and_closes_failed_status(
    tmp_path: Path,
):
    prepared_session = _PreparedRunSessionWithRepromptStandIn(
        initial_run_kind=RunKind.FRESH,
        initial_provider_session_id="provider-fresh",
        reprompt_run_kind=RunKind.RESUME,
        reprompt_provider_session_id="provider-resume",
    )
    status_display = RecordingStatusDisplay()
    work_calls: list[tuple[RunKind, str | None, str]] = []

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> PlannerOutput:
            del role, on_provider_session_id
            work_calls.append((run_kind, session_uuid, prompt))
            raise AgentOutputProtocolError("missing tag")

    async def prompt_factory(*, run_kind: RunKind, container_exec) -> str:
        del container_exec
        assert run_kind is RunKind.FRESH
        return "caller-rendered prompt text"

    with pytest.raises(AgentFailedError) as exc_info:
        asyncio.run(
            invoke_work(
                WorkInvocationRequest(
                    name="Planner",
                    mount_path=tmp_path,
                    role=AgentRole.PLANNER,
                    service=ClaudeService(),
                    model="sonnet",
                    effort="high",
                    output_adapter=ProtocolOutputAdapter(
                        prompt_factory=prompt_factory,
                        reprompt_message="reprompt",
                    ),
                    dependencies=WorkInvocationDependencies(
                        container_workspace="/home/agent/workspace",
                        timeout_retries=0,
                        stage_key_for_role=lambda role: role.value,
                        prepare_session=lambda _request: prepared_session,
                        build_session=lambda *_args: _FakeSession(),
                        build_runner=lambda *_args: cast(
                            ContainerRunner, _FakeRunner()
                        ),
                        get_git_identity=lambda: ("Test User", "test@example.com"),
                    ),
                    status_display=status_display,
                )
            )
        )

    assert exc_info.value.failure_class == "protocol_error"
    assert work_calls == [
        (RunKind.FRESH, "provider-fresh", "caller-rendered prompt text"),
        (RunKind.RESUME, "provider-resume", "reprompt"),
        (RunKind.RESUME, "provider-resume", "reprompt"),
    ]
    assert ("remove", "Planner", "failed", "error") in status_display.calls


def test_work_invocation_text_output_has_no_protocol_reprompt_path(
    tmp_path: Path,
):
    prepared_session = _PreparedRunSessionWithRepromptStandIn(
        initial_run_kind=RunKind.FRESH,
        initial_provider_session_id="provider-fresh",
        reprompt_run_kind=RunKind.RESUME,
        reprompt_provider_session_id="provider-resume",
    )
    status_display = RecordingStatusDisplay()
    work_text_calls: list[tuple[RunKind, str | None, str]] = []

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work_text(
            self,
            prompt: str,
            *,
            role: AgentRole,
            tool_policy,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> str:
            del role, tool_policy, on_provider_session_id
            work_text_calls.append((run_kind, session_uuid, prompt))
            raise AgentOutputProtocolError("text outputs do not reprompt")

    with pytest.raises(AgentOutputProtocolError, match="text outputs do not reprompt"):
        asyncio.run(
            invoke_work(
                WorkInvocationRequest(
                    name="Prompt",
                    mount_path=tmp_path,
                    role=AgentRole.PLANNER,
                    service=ClaudeService(),
                    model="sonnet",
                    effort="high",
                    output_adapter=TextOutputAdapter(prompt="text prompt"),
                    dependencies=WorkInvocationDependencies(
                        container_workspace="/home/agent/workspace",
                        timeout_retries=0,
                        stage_key_for_role=lambda role: role.value,
                        prepare_session=lambda _request: prepared_session,
                        build_session=lambda *_args: _FakeSession(),
                        build_runner=lambda *_args: cast(
                            ContainerRunner, _FakeRunner()
                        ),
                        get_git_identity=lambda: ("Test User", "test@example.com"),
                    ),
                    status_display=status_display,
                )
            )
        )

    assert work_text_calls == [(RunKind.FRESH, "provider-fresh", "text prompt")]
    assert ("remove", "Prompt", "failed", "error") in status_display.calls


def test_resume_run_non_typed_exception_does_not_wipe_session(tmp_path):
    """On consecutive non-typed exceptions during a Resume run, start_fresh is not called — session dir is preserved."""
    _seed_implementer_session(tmp_path)
    session_file = (
        tmp_path / ".pycastle-session" / "implementer" / "claude" / "session.json"
    )
    assert session_file.exists()

    mock_client = _make_docker_client_with_controlled_streams(
        [RuntimeError("first failure"), RuntimeError("second failure")]
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
    )

    with pytest.raises(AgentFailedError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Impl",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )

    assert session_file.exists(), (
        "session.json was wiped but should have been preserved"
    )


def test_fresh_run_non_typed_exception_propagates(tmp_path):
    """A non-typed exception on a Fresh run (no existing session) propagates immediately."""
    mock_client = _make_docker_client_with_controlled_streams(
        [RuntimeError("docker failure")]
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
    )

    with pytest.raises(RuntimeError, match="docker failure"):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Impl",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )


# ── AgentRunner: _build_prompt ────────────────────────────────────────────────


async def _noop_exec(cmd: str) -> str:
    return ""


def _make_build_prompt_cfg(tmp_path: Path) -> Config:
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "coordination").mkdir(exist_ok=True)
    (prompts_dir / "shared").mkdir(exist_ok=True)
    (prompts_dir / "coordination/plan.md").write_text(
        "{{ALL_OPEN_ISSUES_JSON}} {{READY_FOR_AGENT_ISSUES_JSON}}", encoding="utf-8"
    )
    (prompts_dir / "shared/resume.md").write_text("resume-content", encoding="utf-8")
    return Config(logs_dir=tmp_path)


def test_build_prompt_uses_resume_template_on_resume_without_role_flag(tmp_path):
    """On a Resume run without send_role_prompt_on_resume, _build_prompt uses the RESUME template."""
    cfg = _make_build_prompt_cfg(tmp_path)
    runner = AgentRunner({}, cfg, _make_git_service())

    result = asyncio.run(
        runner._build_prompt(
            _PLAN_TEMPLATE,
            _PLAN_SCOPE_ARGS,
            _noop_exec,
            run_kind=RunKind.RESUME,
            send_role_prompt_on_resume=False,
        )
    )

    assert result == "resume-content"


def test_build_prompt_uses_role_template_on_resume_with_send_role_prompt(tmp_path):
    """On a Resume run with send_role_prompt_on_resume=True, _build_prompt uses the role template."""
    cfg = _make_build_prompt_cfg(tmp_path)
    runner = AgentRunner({}, cfg, _make_git_service())

    result = asyncio.run(
        runner._build_prompt(
            _PLAN_TEMPLATE,
            _PLAN_SCOPE_ARGS,
            _noop_exec,
            run_kind=RunKind.RESUME,
            send_role_prompt_on_resume=True,
        )
    )

    assert result == "[] []"


def test_build_prompt_uses_role_template_on_fresh_run(tmp_path):
    """On a Fresh run, _build_prompt renders the role template."""
    cfg = _make_build_prompt_cfg(tmp_path)
    runner = AgentRunner({}, cfg, _make_git_service())

    result = asyncio.run(
        runner._build_prompt(
            _PLAN_TEMPLATE,
            _PLAN_SCOPE_ARGS,
            _noop_exec,
            run_kind=RunKind.FRESH,
            send_role_prompt_on_resume=False,
        )
    )

    assert result == "[] []"


def test_build_prompt_expands_shell_expressions_via_container_exec(tmp_path):
    """_build_prompt passes container_exec to the renderer for shell expression expansion."""
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "shared").mkdir(exist_ok=True)
    (prompts_dir / "shared/resume.md").write_text(
        "Result: !`echo hi`", encoding="utf-8"
    )
    cfg = Config(logs_dir=tmp_path)
    runner = AgentRunner({}, cfg, _make_git_service())

    async def fake_exec(cmd: str) -> str:
        if "echo hi" in cmd:
            return "expanded\n"
        return ""

    result = asyncio.run(
        runner._build_prompt(
            PromptTemplate.RESUME,
            {},
            fake_exec,
            run_kind=RunKind.RESUME,
            send_role_prompt_on_resume=False,
        )
    )

    assert result == "Result: expanded"


# ── HardAgentError: runner cancels token and does NOT mark_exhausted ─────────


def test_agent_runner_cancels_token_on_hard_agent_error(tmp_path):
    """HardAgentError from a 4xx result cancels the CancellationToken and re-raises."""
    mock_client = _make_docker_client(
        [
            b'{"type":"result","is_error":true,"api_error_status":401,'
            b'"result":"API Error: 401 Unauthorized"}\n'
        ]
    )
    token = CancellationToken()
    runner = AgentRunner(
        {}, _make_cfg(tmp_path), _make_git_service(), docker_client=mock_client
    )

    with pytest.raises(HardAgentError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Implement Agent #42",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    token=token,
                )
            )
        )

    assert token.is_cancelled


def test_agent_runner_does_not_call_mark_exhausted_on_hard_agent_error(tmp_path):
    """HardAgentError must NOT mark the account exhausted (request-specific, not account-specific)."""
    from pycastle.services.claude_service import ClaudeService

    mock_client = _make_docker_client(
        [
            b'{"type":"result","is_error":true,"api_error_status":400,'
            b'"result":"API Error: 400 Bad Request"}\n'
        ]
    )
    svc = ClaudeService(accounts=[("primary", "tok-primary")])
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
        service_registry={"claude": svc},
    )

    with pytest.raises(HardAgentError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )

    # Account must still be available — mark_exhausted was NOT called
    assert svc.is_available() is True


def test_agent_runner_claude_reprompt_retries_as_resume_with_same_derived_uuid(
    tmp_path: Path,
):
    from unittest.mock import patch
    from pycastle.agents.output_protocol import PlanParseError
    from pycastle.infrastructure.container_runner import ContainerRunner

    work_calls: list[tuple[RunKind, str | None]] = []
    expected_session_id = RoleSession(
        tmp_path,
        AgentRole.PLANNER,
    ).session_uuid()

    async def _fake_work(
        role, prompt, *, run_kind, session_uuid, on_provider_session_id=None
    ):
        del role, prompt, on_provider_session_id
        work_calls.append((run_kind, session_uuid))
        if len(work_calls) == 1:
            raise PlanParseError("missing required tag")
        return PlannerOutput(issues=[])

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_setup_docker_client(),
        service_registry={"claude": ClaudeService()},
    )

    with patch.object(ContainerRunner, "work", side_effect=_fake_work):
        result = asyncio.run(
            runner.run(
                _run_request(
                    name="Claude",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    role=AgentRole.PLANNER,
                    service="claude",
                )
            )
        )

    assert isinstance(result, PlannerOutput)
    assert work_calls == [
        (RunKind.FRESH, expected_session_id),
        (RunKind.RESUME, expected_session_id),
    ]


def test_agent_runner_codex_fails_with_protocol_error_when_no_thread_id_captured(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    host_auth = home / ".codex" / "auth.json"
    host_auth.parent.mkdir(parents=True)
    host_auth.write_text('{"mode":"oauth"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)

    mock_client = _make_docker_client(
        [
            b'{"type":"item.completed","item":{"type":"agent_message",'
            b'"content":"missing required tag"}}\n',
            b'{"type":"turn.completed","usage":{}}\n',
        ]
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
        service_registry={"codex": CodexService()},
    )

    with pytest.raises(AgentFailedError) as exc_info:
        asyncio.run(
            runner.run(
                _run_request(
                    name="Codex",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    role=AgentRole.PLANNER,
                    service="codex",
                )
            )
        )

    assert exc_info.value.failure_class == "protocol_error"


# ── AgentRunner: protocol-error retry semantics ───────────────────────────────


def _make_setup_docker_client() -> MagicMock:
    """Mock docker client that handles container start and non-streaming setup calls."""
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container
    mock_container.exec_run.return_value = MagicMock(exit_code=0, output=(b"", b""))
    return mock_client


def test_agent_runner_run_returns_success_after_protocol_error_on_first_attempt(
    tmp_path,
):
    from unittest.mock import patch
    from pycastle.agents.output_protocol import PlanParseError
    from pycastle.agents.runner import REPROMPT_MESSAGE
    from pycastle.infrastructure.container_runner import ContainerRunner

    success_output = CommitMessageOutput(message="done")
    work_calls: list[tuple[str, RunKind]] = []

    async def _fake_work(
        role, prompt, *, run_kind, session_uuid, on_provider_session_id=None
    ):
        work_calls.append((prompt, run_kind))
        if len(work_calls) == 1:
            raise PlanParseError("no tag")
        return success_output

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_setup_docker_client(),
    )

    with patch.object(ContainerRunner, "work", side_effect=_fake_work):
        result = asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )

    assert isinstance(result, CommitMessageOutput)
    assert len(work_calls) == 2
    assert work_calls[1][0] == REPROMPT_MESSAGE
    assert work_calls[1][1] == RunKind.RESUME


def test_agent_runner_run_raises_agent_failed_error_after_three_protocol_errors(
    tmp_path,
):
    from unittest.mock import patch
    from pycastle.agents.output_protocol import PromiseParseError
    from pycastle.infrastructure.container_runner import ContainerRunner

    call_count = 0

    async def _fake_work(
        role, prompt, *, run_kind, session_uuid, on_provider_session_id=None
    ):
        nonlocal call_count
        call_count += 1
        raise PromiseParseError("no tag")

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_setup_docker_client(),
    )

    with patch.object(ContainerRunner, "work", side_effect=_fake_work):
        with pytest.raises(AgentFailedError) as exc_info:
            asyncio.run(
                runner.run(
                    _run_request(
                        name="Test",
                        template=_PLAN_TEMPLATE,
                        scope_args=_PLAN_SCOPE_ARGS,
                        mount_path=tmp_path,
                    )
                )
            )

    assert exc_info.value.failure_class == "protocol_error"
    assert call_count == 3


def test_agent_runner_run_does_not_reprompt_when_work_returns_failed_output(tmp_path):
    from unittest.mock import patch
    from pycastle.infrastructure.container_runner import ContainerRunner

    call_count = 0

    async def _fake_work(
        role, prompt, *, run_kind, session_uuid, on_provider_session_id=None
    ):
        nonlocal call_count
        call_count += 1
        return FailedOutput(failure_class="agent_failed")

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_setup_docker_client(),
    )

    with patch.object(ContainerRunner, "work", side_effect=_fake_work):
        with pytest.raises(AgentFailedError):
            asyncio.run(
                runner.run(
                    _run_request(
                        name="Test",
                        template=_PLAN_TEMPLATE,
                        scope_args=_PLAN_SCOPE_ARGS,
                        mount_path=tmp_path,
                    )
                )
            )

    assert call_count == 1


def test_agent_runner_run_decrements_timeout_budget_when_protocol_error_precedes_timeout(
    tmp_path,
):
    from unittest.mock import patch
    from pycastle.agents.output_protocol import PlanParseError
    from pycastle.infrastructure.container_runner import ContainerRunner

    call_count = 0

    async def _fake_work(
        role, prompt, *, run_kind, session_uuid, on_provider_session_id=None
    ):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise PlanParseError("no tag")
        raise AgentTimeoutError("timeout")

    cfg = _make_cfg(tmp_path, timeout_retries=0)
    runner = AgentRunner(
        {}, cfg, _make_git_service(), docker_client=_make_setup_docker_client()
    )

    with patch.object(ContainerRunner, "work", side_effect=_fake_work):
        with pytest.raises(AgentTimeoutError):
            asyncio.run(
                runner.run(
                    _run_request(
                        name="Test",
                        template=_PLAN_TEMPLATE,
                        scope_args=_PLAN_SCOPE_ARGS,
                        mount_path=tmp_path,
                    )
                )
            )
