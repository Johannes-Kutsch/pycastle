import asyncio
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import TypedDict
from unittest.mock import MagicMock, patch

import pytest

from pycastle.agents.output_protocol import PlanParseError
from pycastle.agents.output_protocol import AgentRole
from pycastle.agents.result import CancellationToken
from pycastle.config import Config
from pycastle.errors import (
    AgentTimeoutError,
    HardAgentError,
    TransientAgentError,
    UsageLimitError,
)
from pycastle.infrastructure.container_runner import ContainerRunner
from pycastle.services.claude_service import ClaudeService
from pycastle.services import GitService
from pycastle.services.agent_service import (
    AssistantTurn,
    HardError,
    ParsedTurn,
    Result,
    TransientError,
)
from pycastle.services.provider_session_state import (
    ProviderSessionState,
    ProviderSessionStateRequest,
)
from pycastle.session import RoleSession
from pycastle.session.agent import RunSessionPlan
from pycastle.session import RunKind


def _make_cfg(tmp_path: Path, **kwargs) -> Config:
    return Config(logs_dir=tmp_path, **kwargs)


def _make_git_service() -> MagicMock:
    svc = MagicMock(spec=GitService)
    svc.get_user_name.return_value = "Alice"
    svc.get_user_email.return_value = "alice@example.com"
    return svc


def _make_docker_client(chunks: list[bytes]) -> MagicMock:
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


class _RecordingRuntimeService:
    def __init__(self, name: str, events: Iterable[ParsedTurn] | None = None) -> None:
        self.name = name
        self._events = tuple(events or (Result(text="runtime result"),))
        self.tool_policies: list[object] = []

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
        del role, model, effort, run_kind, session_uuid
        self.tool_policies.append(tool_policy)
        return f"{self.name} exec"

    def build_env(
        self,
        state_dir_container_path: str | None = None,
        token: str | None = None,
    ) -> dict[str, str]:
        del state_dir_container_path, token
        return {}

    def run(
        self,
        lines: Iterable[str],
        on_provider_session_id: Callable[[str], None] | None = None,
    ) -> Iterator[ParsedTurn]:
        del on_provider_session_id
        list(lines)
        yield from self._events

    def is_available(self, now: datetime | None = None) -> bool:
        del now
        return True

    def next_wake_time(self) -> datetime:
        return datetime.max

    def mark_exhausted(self, reset_time: datetime | None) -> None:
        del reset_time

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        del role, namespace
        return None

    def is_resumable(self, state_dir: Path) -> bool:
        del state_dir
        return False

    def provider_session_state(
        self,
        request: ProviderSessionStateRequest,
    ) -> ProviderSessionState:
        del request
        return ProviderSessionState(RunKind.FRESH, None)

    def valid_efforts(self) -> frozenset[str]:
        return frozenset({"medium"})

    def valid_models(self) -> frozenset[str]:
        return frozenset({"gpt-5.4"})


class _SequencedAvailabilityRuntimeService(_RecordingRuntimeService):
    def __init__(self, name: str, availability: Iterable[bool]) -> None:
        super().__init__(name)
        self._availability = iter(availability)

    def is_available(self, now: datetime | None = None) -> bool:
        del now
        return next(self._availability)


class _PlanRecordingClaudeRuntimeService(ClaudeService):
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


class _PlanRecordingRuntimeService(_RecordingRuntimeService):
    def __init__(self, name: str, provider_state: ProviderSessionState) -> None:
        super().__init__(name)
        self._provider_state = provider_state
        self.build_env_state_dir_args: list[str | None] = []

    def build_env(
        self,
        state_dir_container_path: str | None = None,
        token: str | None = None,
    ) -> dict[str, str]:
        del token
        self.build_env_state_dir_args.append(state_dir_container_path)
        return {}

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        del role
        if namespace:
            return f".pycastle-session/implementer/{namespace}/{self.name}/"
        return f".pycastle-session/implementer/{self.name}/"

    def provider_session_state(
        self,
        request: ProviderSessionStateRequest,
    ) -> ProviderSessionState:
        del request
        return self._provider_state


class _TextSuccessRuntimeService(_PlanRecordingRuntimeService):
    def __init__(
        self,
        name: str,
        provider_state: ProviderSessionState,
        *,
        observed_provider_session_id: str,
    ) -> None:
        super().__init__(name, provider_state)
        self._observed_provider_session_id = observed_provider_session_id
        self.command_calls: list[tuple[object, RunKind, str | None]] = []

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
        del role, model, effort
        self.command_calls.append((tool_policy, run_kind, session_uuid))
        return f"{self.name} exec"

    def run(
        self,
        lines: Iterable[str],
        on_provider_session_id: Callable[[str], None] | None = None,
    ) -> Iterator[ParsedTurn]:
        list(lines)
        if on_provider_session_id is not None:
            on_provider_session_id(self._observed_provider_session_id)
        yield Result(text="exact text from adapter")


class _TransientRuntimeService(_PlanRecordingRuntimeService):
    def __init__(
        self,
        name: str,
        provider_state: ProviderSessionState,
        *,
        observed_provider_session_id: str,
        status_code: int | None,
    ) -> None:
        super().__init__(name, provider_state)
        self._observed_provider_session_id = observed_provider_session_id
        self._status_code = status_code
        self.mark_exhausted_calls: list[datetime | None] = []

    def run(
        self,
        lines: Iterable[str],
        on_provider_session_id: Callable[[str], None] | None = None,
    ) -> Iterator[ParsedTurn]:
        list(lines)
        if on_provider_session_id is not None:
            on_provider_session_id(self._observed_provider_session_id)
        yield TransientError(
            status_code=self._status_code,
            raw_message=(
                "API Error: 529 Overloaded"
                if self._status_code is not None
                else "network drop"
            ),
        )

    def mark_exhausted(self, reset_time: datetime | None) -> None:
        self.mark_exhausted_calls.append(reset_time)


class _HardRuntimeService(_PlanRecordingRuntimeService):
    def __init__(
        self,
        name: str,
        provider_state: ProviderSessionState,
        *,
        observed_provider_session_id: str,
        status_code: int,
    ) -> None:
        super().__init__(name, provider_state)
        self._observed_provider_session_id = observed_provider_session_id
        self._status_code = status_code
        self.mark_exhausted_calls: list[datetime | None] = []

    def run(
        self,
        lines: Iterable[str],
        on_provider_session_id: Callable[[str], None] | None = None,
    ) -> Iterator[ParsedTurn]:
        list(lines)
        if on_provider_session_id is not None:
            on_provider_session_id(self._observed_provider_session_id)
        yield HardError(
            status_code=self._status_code,
            raw_message="API Error: 403 Forbidden",
        )

    def mark_exhausted(self, reset_time: datetime | None) -> None:
        self.mark_exhausted_calls.append(reset_time)


class _RuntimeSessionStandIn:
    def __init__(self) -> None:
        self.exec_simple_calls: list[str] = []
        self.written_files: list[tuple[str, str]] = []
        self.stream_commands: list[str] = []

    def __enter__(self) -> "_RuntimeSessionStandIn":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def exec_simple(self, cmd: str) -> str:
        self.exec_simple_calls.append(cmd)
        return ""

    def write_file(self, content: str, path: str) -> None:
        self.written_files.append((content, path))

    def exec_stream(self, cmd: str):
        self.stream_commands.append(cmd)
        return iter(())


class _RuntimeRunnerStandIn:
    def __init__(
        self,
        *,
        cfg: Config,
        git_service: GitService,
        service: _PlanRecordingRuntimeService,
        session: _RuntimeSessionStandIn,
    ) -> None:
        self._cfg = cfg
        self._git_service = git_service
        self._service = service
        self._session = session

    def _resolve_service(self, service_name: str):
        assert service_name == self._service.name
        return self._service

    def _build_session(
        self,
        mount_path: Path,
        service,
        state_dir_container_path: str | None = None,
    ) -> _RuntimeSessionStandIn:
        del mount_path
        assert service is self._service
        service.build_env(state_dir_container_path)
        return self._session


def test_runtime_package_runs_prompt_contract_and_returns_llm_output(tmp_path: Path):
    import pycastle_agent_runtime as runtime

    service = _RecordingRuntimeService("codex")
    runner = runtime.AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client([]),
        service_registry={"codex": service},
    )
    registry = runtime.ServiceRegistry({"codex": service})
    request = runtime.PromptRunRequest(
        name="Runtime Consumer",
        mount_path=tmp_path,
        prompt="Return the final answer only.",
        override=runtime.StageOverride(
            service="missing",
            model="ignored",
            effort="medium",
            fallback=runtime.StageOverride(
                service="codex",
                model="gpt-5.4",
                effort="medium",
            ),
        ),
        tool_policy=runtime.ToolPolicy.PARTIAL,
    )

    result = asyncio.run(
        runtime.run_prompt(runner=runner, service_registry=registry, request=request)
    )

    assert result == "runtime result"
    assert service.tool_policies == [runtime.ToolPolicy.PARTIAL]


def test_runtime_package_returns_assistant_turns_when_service_emits_no_result(
    tmp_path: Path,
):
    import pycastle_agent_runtime as runtime

    service = _RecordingRuntimeService(
        "codex",
        events=(
            AssistantTurn(text="first turn"),
            AssistantTurn(text="second turn"),
        ),
    )
    runner = runtime.AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client([]),
        service_registry={"codex": service},
    )
    registry = runtime.ServiceRegistry({"codex": service})
    request = runtime.PromptRunRequest(
        mount_path=tmp_path,
        prompt="Return the final answer only.",
        override=runtime.StageOverride(
            service="codex",
            model="gpt-5.4",
            effort="medium",
        ),
    )

    result = asyncio.run(
        runtime.run_prompt(runner=runner, service_registry=registry, request=request)
    )

    assert result == "first turn\nsecond turn"
    assert service.tool_policies == [runtime.ToolPolicy.FULL]


def test_runtime_package_owns_service_selection_contract() -> None:
    import pycastle_agent_runtime as runtime

    primary = _RecordingRuntimeService("codex")
    fallback = _RecordingRuntimeService("claude")

    def _unavailable(now: datetime | None = None) -> bool:
        del now
        return False

    primary.is_available = _unavailable  # type: ignore[method-assign]
    registry = runtime.ServiceRegistry({"codex": primary, "claude": fallback})
    override = runtime.StageOverride(
        service="missing",
        model="ignored",
        effort="medium",
        fallback=runtime.StageOverride(
            service="codex",
            model="gpt-5.4",
            effort="medium",
            fallback=runtime.StageOverride(
                service="claude",
                model="sonnet",
                effort="high",
            ),
        ),
    )

    resolved = registry.resolve(override, datetime(2026, 1, 1))

    assert runtime.ServiceRegistry.__module__.startswith("pycastle_agent_runtime")
    assert resolved == runtime.StageOverride(
        service="claude",
        model="sonnet",
        effort="high",
    )


def test_runtime_package_service_registry_snapshots_availability_per_configured_service() -> (
    None
):
    import pycastle_agent_runtime as runtime

    registry = runtime.ServiceRegistry(
        {
            "codex": _SequencedAvailabilityRuntimeService("codex", [False, True]),
            "claude": _RecordingRuntimeService("claude"),
        }
    )
    override = runtime.StageOverride(
        service="codex",
        model="gpt-5.4",
        effort="medium",
        fallback=runtime.StageOverride(
            service="claude",
            model="sonnet",
            effort="high",
            fallback=runtime.StageOverride(
                service="codex",
                model="gpt-5.5",
                effort="high",
            ),
        ),
    )

    resolved = registry.resolve(override, datetime(2026, 1, 1))

    assert resolved == runtime.StageOverride(
        service="claude",
        model="sonnet",
        effort="high",
        fallback=runtime.StageOverride(
            service="codex",
            model="gpt-5.5",
            effort="high",
        ),
    )


def test_runtime_package_run_prompt_raises_usage_limit_when_token_pre_cancelled(
    tmp_path: Path,
):
    import pycastle_agent_runtime as runtime

    service = _RecordingRuntimeService("codex")
    docker_client = _make_docker_client([])
    runner = runtime.AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=docker_client,
        service_registry={"codex": service},
    )
    registry = runtime.ServiceRegistry({"codex": service})
    token = CancellationToken()
    token.cancel()
    request = runtime.PromptRunRequest(
        mount_path=tmp_path,
        prompt="Return the final answer only.",
        override=runtime.StageOverride(
            service="codex",
            model="gpt-5.4",
            effort="medium",
        ),
        token=token,
    )

    with pytest.raises(UsageLimitError):
        asyncio.run(
            runtime.run_prompt(
                runner=runner, service_registry=registry, request=request
            )
        )

    docker_client.containers.run.assert_not_called()


def test_runtime_package_run_prompt_uses_namespaced_state_dir_for_claude(
    tmp_path: Path,
):
    import pycastle_agent_runtime as runtime

    service = _PlanRecordingClaudeRuntimeService()
    runner = runtime.AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client([]),
        service_registry={"claude": service},
    )
    registry = runtime.ServiceRegistry({"claude": service})
    request = runtime.PromptRunRequest(
        mount_path=tmp_path,
        prompt="Return the final answer only.",
        override=runtime.StageOverride(
            service="claude",
            model="sonnet",
            effort="medium",
        ),
        session_namespace="main",
    )

    with patch.object(ContainerRunner, "work_text", return_value="runtime result"):
        result = asyncio.run(
            runtime.run_prompt(
                runner=runner, service_registry=registry, request=request
            )
        )

    assert result == "runtime result"
    assert service.build_env_state_dir_args == [
        "/home/agent/workspace/.pycastle-session/implementer/main/claude/"
    ]


def test_runtime_package_run_prompt_uses_supplied_run_session_plan(
    tmp_path: Path,
):
    import pycastle_agent_runtime as runtime

    state_dir = tmp_path / ".pycastle-session" / "implementer" / "main" / "claude"
    state_dir.mkdir(parents=True)
    (state_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")

    service = _PlanRecordingClaudeRuntimeService()
    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="main",
        service=service,
    )
    service.fail_provider_session_state = True
    runner = runtime.AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client([]),
        service_registry={"claude": service},
    )
    registry = runtime.ServiceRegistry({"claude": service})
    request = runtime.PromptRunRequest(
        mount_path=tmp_path,
        prompt="Return the final answer only.",
        override=runtime.StageOverride(
            service="claude",
            model="sonnet",
            effort="medium",
        ),
        session_namespace="main",
        run_session_plan=plan,
    )
    work_calls: list[tuple[RunKind, str | None]] = []

    async def _fake_work_text(
        prompt: str,
        *,
        role: AgentRole = AgentRole.IMPLEMENTER,
        tool_policy=None,
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
        on_provider_session_id=None,
    ) -> str:
        del prompt, role, tool_policy, on_provider_session_id
        work_calls.append((run_kind, session_uuid))
        return "runtime result"

    with patch.object(ContainerRunner, "work_text", side_effect=_fake_work_text):
        result = asyncio.run(
            runtime.run_prompt(
                runner=runner, service_registry=registry, request=request
            )
        )

    assert result == "runtime result"
    assert work_calls == [(RunKind.RESUME, plan.provider_session_id)]
    assert service.build_env_state_dir_args == [
        "/home/agent/workspace/.pycastle-session/implementer/main/claude/"
    ]


def test_runtime_package_run_prompt_retries_timeout_and_resumes_work_session(
    tmp_path: Path,
):
    import pycastle_agent_runtime as runtime

    state_dir = tmp_path / ".pycastle-session" / "implementer" / "main" / "claude"
    state_dir.mkdir(parents=True)
    (state_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")

    service = _PlanRecordingClaudeRuntimeService()
    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="main",
        service=service,
    )
    service.fail_provider_session_state = True
    status_display = MagicMock()
    runner = runtime.AgentRunner(
        {},
        _make_cfg(tmp_path, timeout_retries=1),
        _make_git_service(),
        docker_client=_make_docker_client([]),
        service_registry={"claude": service},
    )
    registry = runtime.ServiceRegistry({"claude": service})
    request = runtime.PromptRunRequest(
        mount_path=tmp_path,
        prompt="Return the final answer only.",
        override=runtime.StageOverride(
            service="claude",
            model="sonnet",
            effort="medium",
        ),
        status_display=status_display,
        session_namespace="main",
        run_session_plan=plan,
    )
    work_calls: list[tuple[str, RunKind, str | None]] = []

    async def _fake_work_text(
        prompt: str,
        *,
        role: AgentRole = AgentRole.IMPLEMENTER,
        tool_policy=None,
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
        on_provider_session_id=None,
    ) -> str:
        del role, tool_policy, on_provider_session_id
        work_calls.append((prompt, run_kind, session_uuid))
        if len(work_calls) == 1:
            raise AgentTimeoutError("timeout")
        return "runtime result"

    with patch.object(ContainerRunner, "work_text", side_effect=_fake_work_text):
        result = asyncio.run(
            runtime.run_prompt(
                runner=runner, service_registry=registry, request=request
            )
        )

    assert result == "runtime result"
    assert work_calls == [
        ("Return the final answer only.", RunKind.RESUME, plan.provider_session_id),
        ("Return the final answer only.", RunKind.RESUME, plan.provider_session_id),
    ]
    status_display.print.assert_called_once_with(
        "Runtime Agent",
        "Timeout — restarting (attempt 1/1)",
    )


def test_runtime_package_run_prompt_does_not_reprompt_on_protocol_error(
    tmp_path: Path,
):
    import pycastle_agent_runtime as runtime

    service = _RecordingRuntimeService("codex")
    runner = runtime.AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client([]),
        service_registry={"codex": service},
    )
    registry = runtime.ServiceRegistry({"codex": service})
    request = runtime.PromptRunRequest(
        mount_path=tmp_path,
        prompt="Return the final answer only.",
        override=runtime.StageOverride(
            service="codex",
            model="gpt-5.4",
            effort="medium",
        ),
    )
    work_calls: list[str] = []

    async def _fake_work_text(
        prompt: str,
        *,
        role: AgentRole = AgentRole.IMPLEMENTER,
        tool_policy=None,
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
        on_provider_session_id=None,
    ) -> str:
        del role, tool_policy, run_kind, session_uuid, on_provider_session_id
        work_calls.append(prompt)
        raise PlanParseError("missing required tag")

    with patch.object(ContainerRunner, "work_text", side_effect=_fake_work_text):
        with pytest.raises(PlanParseError, match="missing required tag"):
            asyncio.run(
                runtime.run_prompt(
                    runner=runner, service_registry=registry, request=request
                )
            )

    assert work_calls == ["Return the final answer only."]


@pytest.mark.parametrize(
    ("provider_state", "prepared_provider_session_id", "observed_provider_session_id"),
    [
        (
            ProviderSessionState(RunKind.FRESH, "provider-fresh"),
            "provider-fresh",
            "provider-fresh-runtime",
        ),
        (
            ProviderSessionState(RunKind.RESUME, "provider-resume"),
            "provider-resume",
            "provider-resume-runtime",
        ),
    ],
)
def test_runtime_package_run_prompt_records_provider_session_metadata_for_text_success(
    tmp_path: Path,
    provider_state: ProviderSessionState,
    prepared_provider_session_id: str,
    observed_provider_session_id: str,
):
    import pycastle_agent_runtime as runtime

    service = _TextSuccessRuntimeService(
        "fake",
        provider_state,
        observed_provider_session_id=observed_provider_session_id,
    )
    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="main",
        service=service,
    )
    session = _RuntimeSessionStandIn()
    runner = _RuntimeRunnerStandIn(
        cfg=_make_cfg(tmp_path),
        git_service=_make_git_service(),
        service=service,
        session=session,
    )
    registry = runtime.ServiceRegistry({"fake": service})
    request = runtime.PromptRunRequest(
        name="Runtime Consumer",
        mount_path=tmp_path,
        prompt="Return the final answer only.",
        override=runtime.StageOverride(
            service="fake",
            model="gpt-5.4",
            effort="medium",
        ),
        tool_policy=runtime.ToolPolicy.PARTIAL,
        session_namespace="main",
        run_session_plan=plan,
    )

    result = asyncio.run(
        runtime.run_prompt(runner=runner, service_registry=registry, request=request)
    )

    assert result == "exact text from adapter"
    assert service.command_calls == [
        (
            runtime.ToolPolicy.PARTIAL,
            provider_state.run_kind,
            prepared_provider_session_id,
        )
    ]
    assert session.written_files == [
        ("Return the final answer only.", "/tmp/.pycastle_prompt")
    ]
    assert service.build_env_state_dir_args == [
        "/home/agent/workspace/.pycastle-session/implementer/main/fake/"
    ]
    assert RoleSession(
        tmp_path,
        AgentRole.IMPLEMENTER,
        "main",
    ).service_session_metadata("fake") == {
        "service": "fake",
        "provider_session_id": observed_provider_session_id,
    }


def test_runtime_package_run_prompt_transient_failure_cancels_token_preserves_service_availability_and_skips_success_metadata(
    tmp_path: Path,
):
    import pycastle_agent_runtime as runtime

    status_display = MagicMock()
    token = CancellationToken()
    service = _TransientRuntimeService(
        "fake",
        ProviderSessionState(RunKind.RESUME, "provider-resume"),
        observed_provider_session_id="provider-runtime",
        status_code=529,
    )
    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="main",
        service=service,
    )
    session = _RuntimeSessionStandIn()
    runner = _RuntimeRunnerStandIn(
        cfg=_make_cfg(tmp_path),
        git_service=_make_git_service(),
        service=service,
        session=session,
    )
    registry = runtime.ServiceRegistry({"fake": service})
    request = runtime.PromptRunRequest(
        name="Runtime Consumer",
        mount_path=tmp_path,
        prompt="Return the final answer only.",
        override=runtime.StageOverride(
            service="fake",
            model="gpt-5.4",
            effort="medium",
        ),
        status_display=status_display,
        token=token,
        session_namespace="main",
        run_session_plan=plan,
    )

    with pytest.raises(TransientAgentError) as exc_info:
        asyncio.run(
            runtime.run_prompt(
                runner=runner, service_registry=registry, request=request
            )
        )

    assert exc_info.value.status_code == 529
    assert token.is_cancelled is True
    assert service.mark_exhausted_calls == []
    status_display.print.assert_any_call(
        "Runtime Consumer",
        "transient API error: status 529",
    )
    assert session.written_files == [
        ("Return the final answer only.", "/tmp/.pycastle_prompt")
    ]
    assert (
        RoleSession(
            tmp_path,
            AgentRole.IMPLEMENTER,
            "main",
        ).service_session_metadata("fake")
        is None
    )


def test_runtime_package_run_prompt_hard_failure_cancels_token_annotates_context_and_skips_success_metadata(
    tmp_path: Path,
):
    import pycastle_agent_runtime as runtime

    token = CancellationToken()
    service = _HardRuntimeService(
        "fake",
        ProviderSessionState(RunKind.RESUME, "provider-resume"),
        observed_provider_session_id="provider-runtime",
        status_code=403,
    )
    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="main",
        service=service,
    )
    session = _RuntimeSessionStandIn()
    runner = _RuntimeRunnerStandIn(
        cfg=_make_cfg(tmp_path),
        git_service=_make_git_service(),
        service=service,
        session=session,
    )
    registry = runtime.ServiceRegistry({"fake": service})
    request = runtime.PromptRunRequest(
        name="Runtime Consumer",
        mount_path=tmp_path,
        prompt="Return the final answer only.",
        override=runtime.StageOverride(
            service="fake",
            model="gpt-5.4",
            effort="medium",
        ),
        token=token,
        session_namespace="main",
        run_session_plan=plan,
    )

    with pytest.raises(HardAgentError) as exc_info:
        asyncio.run(
            runtime.run_prompt(
                runner=runner, service_registry=registry, request=request
            )
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.caller == "Runtime Consumer"
    assert exc_info.value.service_name == "fake"
    assert token.is_cancelled is True
    assert service.mark_exhausted_calls == []
    assert session.written_files == [
        ("Return the final answer only.", "/tmp/.pycastle_prompt")
    ]
    assert (
        RoleSession(
            tmp_path,
            AgentRole.IMPLEMENTER,
            "main",
        ).service_session_metadata("fake")
        is None
    )


class _ExpectedFallback(TypedDict):
    service: str
    model: str
    effort: str


class _ExpectedSelection(TypedDict):
    service: str
    model: str
    effort: str
    fallback: _ExpectedFallback | None


@pytest.mark.parametrize(
    ("available_service_names", "expected"),
    [
        (
            ("codex", "claude"),
            {
                "service": "codex",
                "model": "gpt-5.4",
                "effort": "medium",
                "fallback": {
                    "service": "claude",
                    "model": "sonnet",
                    "effort": "high",
                },
            },
        ),
        (
            ("claude",),
            {
                "service": "claude",
                "model": "sonnet",
                "effort": "high",
                "fallback": None,
            },
        ),
        (
            (),
            {
                "service": "codex",
                "model": "gpt-5.4",
                "effort": "medium",
                "fallback": {
                    "service": "claude",
                    "model": "sonnet",
                    "effort": "high",
                },
            },
        ),
    ],
)
def test_runtime_package_exports_stage_selection_contract(
    available_service_names: tuple[str, ...],
    expected: _ExpectedSelection,
) -> None:
    import pycastle_agent_runtime as runtime

    override = runtime.StageOverride(
        service="missing",
        model="ignored",
        effort="medium",
        fallback=runtime.StageOverride(
            service="codex",
            model="gpt-5.4",
            effort="medium",
            fallback=runtime.StageOverride(
                service="claude",
                model="sonnet",
                effort="high",
            ),
        ),
    )

    selection = runtime.select_configured_candidate_chain(
        override,
        configured_service_names=("codex", "claude"),
        available_service_names=available_service_names,
    )

    assert selection.has_configured_candidate is True
    fallback = expected["fallback"]
    assert selection.selected_chain == runtime.StageOverride(
        service=str(expected["service"]),
        model=str(expected["model"]),
        effort=str(expected["effort"]),
        fallback=(
            None
            if fallback is None
            else runtime.StageOverride(
                service=str(fallback["service"]),
                model=str(fallback["model"]),
                effort=str(fallback["effort"]),
            )
        ),
    )


def test_runtime_package_stage_selection_reports_when_no_candidate_is_configured() -> (
    None
):
    import pycastle_agent_runtime as runtime

    override = runtime.StageOverride(
        service="missing-primary",
        model="ignored",
        effort="medium",
        fallback=runtime.StageOverride(
            service="missing-fallback",
            model="ignored",
            effort="high",
        ),
    )

    selection = runtime.select_configured_candidate_chain(
        override,
        configured_service_names=("codex", "claude"),
        available_service_names=("codex",),
    )

    assert selection == runtime.ConfiguredCandidateSelection(
        has_configured_candidate=False,
        selected_chain=None,
    )


class _StateDirRecordingRuntimeService(_RecordingRuntimeService):
    def __init__(self, name: str, *, relpath: str) -> None:
        super().__init__(name)
        self._relpath = relpath
        self.state_dir_container_paths: list[str | None] = []

    def build_env(
        self,
        state_dir_container_path: str | None = None,
        token: str | None = None,
    ) -> dict[str, str]:
        del token
        self.state_dir_container_paths.append(state_dir_container_path)
        return {}

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        del role, namespace
        return self._relpath


def test_runtime_package_orchestration_entrypoint_owns_service_selection_session_boundary_and_logging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import pycastle_agent_runtime as runtime

    fake_home = tmp_path / "home"
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / ".codex" / "auth.json").write_text(
        '{"access_token":"tok"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(fake_home))

    requested_service = _StateDirRecordingRuntimeService(
        "codex",
        relpath=".pycastle-session/implementer/codex/",
    )
    fallback_service = _RecordingRuntimeService("claude")

    def _unavailable(now: datetime | None = None) -> bool:
        del now
        return False

    fallback_service.is_available = _unavailable  # type: ignore[method-assign]

    prompt_runtime = runtime.PromptRuntime(
        env={},
        cfg=_make_cfg(tmp_path),
        git_service=_make_git_service(),
        docker_client=_make_docker_client([b'{"result":"runtime result"}\n']),
        service_registry={
            "codex": requested_service,
            "claude": fallback_service,
        },
    )
    request = runtime.PromptRunRequest(
        name="Runtime Consumer",
        mount_path=tmp_path,
        prompt="Return the final answer only.",
        override=runtime.StageOverride(
            service="claude",
            model="sonnet",
            effort="high",
            fallback=runtime.StageOverride(
                service="codex",
                model="gpt-5.4",
                effort="medium",
            ),
        ),
        tool_policy=runtime.ToolPolicy.PARTIAL,
    )

    result = asyncio.run(prompt_runtime.run_prompt(request))

    assert result == "runtime result"
    assert requested_service.tool_policies == [runtime.ToolPolicy.PARTIAL]
    [state_dir_container_path] = requested_service.state_dir_container_paths
    assert state_dir_container_path is not None
    assert state_dir_container_path.rstrip("/") == (
        "/home/agent/workspace/.pycastle-session/implementer/codex"
    )
    assert (tmp_path / ".pycastle-session" / "implementer" / "codex").is_dir()

    [log_path] = list(tmp_path.glob("runtime-consumer-*.log"))
    log_text = log_path.read_text(encoding="utf-8")
    assert '"prompt": "Return the final answer only."' in log_text
    assert '"result":"runtime result"' in log_text


def test_runtime_package_exports_orchestration_entrypoint() -> None:
    import pycastle_agent_runtime as runtime

    assert runtime.run.__module__ == "pycastle_agent_runtime.orchestration"
