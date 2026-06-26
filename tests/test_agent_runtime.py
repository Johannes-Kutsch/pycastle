import asyncio
import json
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict, cast
from unittest.mock import MagicMock

import pytest
import pycastle.errors as pycastle_errors
import pycastle.execution_contracts as execution_contracts_module
import pycastle.provider_errors as provider_errors_module
import pycastle.runtime as runtime_module
import pycastle.services.service_registry as service_registry_module

from pycastle.agents._work_invocation import (
    TextOutputAdapter,
    WorkExecutionAdapter,
    WorkInvocationDependencies,
    WorkInvocationRequest,
    invoke_work,
)
from pycastle.agents.runner import AgentRunner
from pycastle.agents.output_protocol import AgentOutput, AgentRole, CompletionOutput
from pycastle import parsed_event_reducer
from pycastle.provider_session_adapter import (
    ProviderSessionPlanningFacts,
    ProviderSessionPlanningRequest,
)
from pycastle.errors import (
    AgentCredentialFailureError,
    AgentTimeoutError,
    HardAgentError,
    RuntimeConfigurationError,
    TransientAgentError,
    UsageLimitError,
)
from pycastle.work import RunSessionPlan, reduce_text_output_events
from pycastle.usage_limit_decision import (
    ContinueNow,
    PermanentlyExhausted,
    Stop,
    TemporaryUsageLimit,
    decide_usage_limit_continuation,
)
from pycastle.runtime_session import (
    ProviderSessionPreferences,
    ProviderSessionPreferencesRequest,
    ProviderSessionState,
    ProviderSessionStateRequest,
)
from pycastle.session.service_session_store import (
    load_service_session_metadata,
    save_service_session_id,
)
from pycastle.session_planning import (
    ProviderRunStatePlanRequest,
    ResidentSessionPlanRequest,
    plan_provider_run_state,
    plan_resident_session,
)
from pycastle.config.types import StageOverride
from pycastle.config import Config
from pycastle.services.claude_service import ClaudeService
from pycastle.services import GitService
from pycastle.services.agent_service import (
    AssistantTurn,
    CredentialFailure,
    HardError,
    ParsedTurn,
    PromptTokens,
    Result,
    TransientError,
    UnsupportedTokens,
    UsageLimit,
)
from pycastle.services.service_registry import ServiceRegistry
from pycastle.stage_priority_chain import (
    ChainEntry,
    ConfiguredCandidateSelection,
    render_chain_label,
    select_configured_candidate_chain,
)
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


def _managed_mount(repo_root: Path, name: str = "issue-1") -> Path:
    if (
        repo_root.parent.name == ".worktrees"
        and repo_root.parent.parent.name == "pycastle"
    ):
        repo_root.mkdir(parents=True, exist_ok=True)
        return repo_root
    mount_path = repo_root / "pycastle" / ".worktrees" / name
    mount_path.mkdir(parents=True, exist_ok=True)
    return mount_path


def _runtime_namespace() -> SimpleNamespace:
    return SimpleNamespace(
        AgentRole=AgentRole,
        CancellationToken=runtime_module.CancellationToken,
        ChainEntry=ChainEntry,
        ConfiguredCandidateSelection=ConfiguredCandidateSelection,
        ContinueNow=ContinueNow,
        OneShotRunRequest=runtime_module.OneShotRunRequest,
        OneShotRunResult=runtime_module.OneShotRunResult,
        OneShotRuntimeMetadata=runtime_module.OneShotRuntimeMetadata,
        PromptRunRequest=runtime_module.PromptRunRequest,
        PromptRunSession=runtime_module.PromptRunSession,
        PromptRuntime=runtime_module.PromptRuntime,
        ProviderRunStatePlanRequest=ProviderRunStatePlanRequest,
        ProviderSessionPreferences=ProviderSessionPreferences,
        ProviderSessionState=ProviderSessionState,
        ProviderSessionStateRequest=ProviderSessionStateRequest,
        ResidentRunRequest=runtime_module.ResidentRunRequest,
        ResidentRunResult=runtime_module.ResidentRunResult,
        ResidentRuntimeMetadata=runtime_module.ResidentRuntimeMetadata,
        ResidentSessionPlanRequest=ResidentSessionPlanRequest,
        RunKind=RunKind,
        ServiceRegistry=ServiceRegistry,
        StageOverride=StageOverride,
        Stop=Stop,
        PermanentlyExhausted=PermanentlyExhausted,
        TemporaryUsageLimit=TemporaryUsageLimit,
        ToolPolicy=runtime_module.ToolPolicy,
        WorktreeMount=runtime_module.WorktreeMount,
        decide_usage_limit_continuation=decide_usage_limit_continuation,
        errors=pycastle_errors,
        execution_contracts=execution_contracts_module,
        parsed_event_reducer=parsed_event_reducer,
        plan_provider_run_state=plan_provider_run_state,
        plan_resident_session=plan_resident_session,
        provider_errors=provider_errors_module,
        render_chain_label=render_chain_label,
        run_one_shot=runtime_module.run_one_shot,
        run_prompt=runtime_module.run_prompt,
        run_resident_prompt=runtime_module.run_resident_prompt,
        select_configured_candidate_chain=select_configured_candidate_chain,
        service_registry=service_registry_module,
    )


def _runtime_module_imported_application_modules(
    repo_root: Path, module_name: str
) -> list[str]:
    pytest.skip("runtime-owned surfaces moved into pycastle")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""
                import importlib
                import json
                import sys

                importlib.import_module({module_name!r})

                forbidden_prefixes = (
                    "pycastle.agents",
                    "pycastle.infrastructure",
                    "pycastle.iteration",
                    "pycastle.prompts",
                    "pycastle.services",
                    "pycastle.session",
                )
                imported = sorted(
                    name
                    for name in sys.modules
                    if any(
                        name == prefix or name.startswith(f"{{prefix}}.")
                        for prefix in forbidden_prefixes
                    )
                )
                print(json.dumps(imported))
                """
            ),
        ],
        cwd=repo_root,
        capture_output=True,
        check=True,
        text=True,
    )
    return json.loads(result.stdout)


def _standalone_runtime_agent_log_result(
    repo_root: Path, effective_logs_dir: Path
) -> dict[str, object]:
    pytest.skip("runtime-owned surfaces moved into pycastle")
    return _standalone_runtime_agent_log_result_with_timezone(
        repo_root,
        effective_logs_dir,
        tz_name="UTC",
    )


def _standalone_runtime_agent_log_result_with_timezone(
    repo_root: Path,
    effective_logs_dir: Path,
    *,
    tz_name: str,
) -> dict[str, object]:
    pytest.skip("runtime-owned surfaces moved into pycastle")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""
                import importlib.abc
                import json
                import sys
                from datetime import datetime, timezone
                from pathlib import Path
                from zoneinfo import ZoneInfo

                class _BlockPycastle(importlib.abc.MetaPathFinder):
                    def find_spec(self, fullname, path=None, target=None):
                        del path, target
                        if fullname == "pycastle" or fullname.startswith("pycastle."):
                            raise ModuleNotFoundError(
                                f"blocked test import: {{fullname}}"
                            )
                        return None

                sys.meta_path.insert(0, _BlockPycastle())

                from pycastle.infrastructure.agent_invocation_log import AgentInvocationLog
                from pycastle.agents.output_protocol import AgentRole
                from pycastle.runtime_session import RunKind

                logs_dir = Path({str(effective_logs_dir)!r})
                fixed_dt = datetime(
                    2026, 5, 17, 14, 30, tzinfo=timezone.utc
                ).astimezone(ZoneInfo({tz_name!r}))
                log = AgentInvocationLog(
                    now_local=lambda: fixed_dt
                )
                log_path = log.reserve(
                    agent_name="Standalone Runtime",
                    effective_logs_dir=logs_dir,
                )
                log.append_work_invocation(
                    log_path=log_path,
                    role=AgentRole.IMPLEMENTER,
                    run_kind=RunKind.FRESH,
                    session_uuid="provider-session-123",
                    prompt="already rendered prompt",
                    provider_bytes=b'{{"type":"result","result":"done"}}\\n',
                )
                print(
                    json.dumps(
                        {{
                            "log_path": str(log_path),
                            "log_lines": log_path.read_text(encoding="utf-8").splitlines(),
                        }}
                    )
                )
                """
            ),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


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

    def provider_session_preferences(
        self,
        request: ProviderSessionPreferencesRequest,
    ) -> ProviderSessionPreferences:
        del request
        return ProviderSessionPreferences()

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

    def provider_session_preferences(
        self,
        request: ProviderSessionPreferencesRequest,
    ) -> ProviderSessionPreferences:
        del request
        return ProviderSessionPreferences()

    def provider_session_state(
        self,
        request: ProviderSessionStateRequest,
    ) -> ProviderSessionState:
        del request
        return self._provider_state


class _ExecutionOnlyRuntimeService:
    def __init__(self, name: str) -> None:
        self.name = name

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
        yield Result(text="runtime result")

    def is_available(self, now: datetime | None = None) -> bool:
        del now
        return True

    def next_wake_time(self) -> datetime:
        return datetime.max

    def mark_exhausted(self, reset_time: datetime | None) -> None:
        del reset_time

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        del role
        if namespace:
            return f".pycastle-session/implementer/{namespace}/{self.name}/"
        return f".pycastle-session/implementer/{self.name}/"

    def is_resumable(self, state_dir: Path) -> bool:
        del state_dir
        return True

    def valid_efforts(self) -> frozenset[str]:
        return frozenset({"medium"})

    def valid_models(self) -> frozenset[str]:
        return frozenset({"gpt-5.4"})


class _RecordingProviderSessionAdapter:
    def __init__(
        self,
        provider_state: ProviderSessionState,
        *,
        service_name: str = "generic",
    ) -> None:
        self._provider_state = provider_state
        self._service_name = service_name
        self.planning_requests: list[ProviderSessionPlanningRequest] = []
        self.preferences_requests: list[ProviderSessionPreferencesRequest] = []
        self.state_requests: list[ProviderSessionStateRequest] = []
        self.prepare_calls: list[tuple[Path | None, object | None]] = []
        self.record_calls: list[tuple[str, Path | None]] = []
        self._planning_facts_provider_state_dir: Path | None = None

    @property
    def service_name(self) -> str:
        return self._service_name

    def provider_session_planning_facts(
        self,
        request: ProviderSessionPlanningRequest,
    ) -> ProviderSessionPlanningFacts:
        self.planning_requests.append(request)
        provider_state_dir = self._planning_facts_provider_state_dir or (
            request.worktree / ".pycastle-session/implementer/main/generic"
        )
        return ProviderSessionPlanningFacts(
            state_dir_relpath=".pycastle-session/implementer/main/generic/",
            provider_state_dir=provider_state_dir,
            has_resumable_provider_state=True,
        )

    def set_planning_facts_provider_state_dir(self, provider_state_dir: Path) -> None:
        self._planning_facts_provider_state_dir = provider_state_dir

    def provider_session_preferences(
        self,
        request: ProviderSessionPreferencesRequest,
    ) -> ProviderSessionPreferences:
        self.preferences_requests.append(request)
        return ProviderSessionPreferences(
            preferred_provider_session_id="preferred-session-id"
        )

    def provider_session_state(
        self,
        request: ProviderSessionStateRequest,
    ) -> ProviderSessionState:
        self.state_requests.append(request)
        return self._provider_state

    def prepare_local_provider_run_state(
        self,
        provider_state_dir: Path | None,
        auth_seed_action: Any = None,
    ) -> None:
        self.prepare_calls.append((provider_state_dir, auth_seed_action))

    def record_provider_session_id(
        self,
        *,
        role_session: Any,
        provider_session_id: str,
        service_state_dir: Path | None = None,
    ) -> None:
        role_session.save_service_session_id(self.service_name, provider_session_id)
        self.record_calls.append((provider_session_id, service_state_dir))

    def recover_provider_session_id(
        self,
        provider_state_dir: Path | None,
    ) -> str | None:
        del provider_state_dir
        return None

    def is_exact_resumable_provider_session(
        self,
        *,
        provider_session_id: str | None,
        provider_state_dir: Path | None,
    ) -> bool:
        return provider_session_id is not None and provider_state_dir is not None


class _ServiceBackedRuntimeProviderSessionAdapter:
    def __init__(self, service: Any) -> None:
        self._service = service

    @property
    def service_name(self) -> str:
        return self._service.name

    def provider_session_planning_facts(
        self,
        request: ProviderSessionPlanningRequest,
    ) -> ProviderSessionPlanningFacts:
        state_dir_relpath = self._service.state_dir_relpath(
            request.role,
            request.namespace,
        )
        provider_state_dir = (
            None
            if state_dir_relpath is None
            else request.worktree / state_dir_relpath.rstrip("/")
        )
        has_resumable_provider_state = (
            provider_state_dir is not None
            and self._service.is_resumable(provider_state_dir)
        )
        return ProviderSessionPlanningFacts(
            state_dir_relpath=state_dir_relpath,
            provider_state_dir=provider_state_dir,
            has_resumable_provider_state=has_resumable_provider_state,
        )

    def provider_session_preferences(
        self,
        request: ProviderSessionPreferencesRequest,
    ) -> ProviderSessionPreferences:
        return self._service.provider_session_preferences(request)

    def provider_session_state(
        self,
        request: ProviderSessionStateRequest,
    ) -> ProviderSessionState:
        return self._service.provider_session_state(request)

    def prepare_local_provider_run_state(
        self,
        provider_state_dir: Path | None,
        auth_seed_action: Any = None,
    ) -> None:
        if provider_state_dir is not None:
            provider_state_dir.mkdir(parents=True, exist_ok=True)
        if auth_seed_action is not None:
            auth_seed_action.apply()

    def record_provider_session_id(
        self,
        *,
        role_session: Any,
        provider_session_id: str,
        service_state_dir: Path | None = None,
    ) -> None:
        del service_state_dir
        role_session.save_service_session_id(self.service_name, provider_session_id)

    def recover_provider_session_id(
        self,
        provider_state_dir: Path | None,
    ) -> str | None:
        del provider_state_dir
        return None

    def is_exact_resumable_provider_session(
        self,
        *,
        provider_session_id: str | None,
        provider_state_dir: Path | None,
    ) -> bool:
        return provider_session_id is not None and provider_state_dir is not None


class _RuntimeServiceSessionState:
    def __init__(
        self,
        *,
        state_dir: Path | None,
        has_resumable_provider_state: bool,
        state_dir_relpath: str | None,
    ) -> None:
        self.state_dir = state_dir
        self.has_resumable_provider_state = has_resumable_provider_state
        self.state_dir_relpath = state_dir_relpath


class _RuntimeRoleSessionStandIn:
    def __init__(self, service_state: _RuntimeServiceSessionState) -> None:
        self._service_state = service_state
        self._service_session_ids: dict[str, str] = {}
        self.saved_service_session_ids: list[tuple[str, str]] = []
        self.recorded_success_metadata: list[tuple[str, str | None]] = []

    def service_session_state(
        self,
        service: _RecordingRuntimeService,
    ) -> _RuntimeServiceSessionState:
        del service
        return self._service_state

    def session_uuid(self) -> str:
        return "runtime-session-uuid"

    def service_session_id(self, service_name: str) -> str | None:
        return self._service_session_ids.get(service_name)

    def save_service_session_id(self, service_name: str, session_id: str) -> None:
        self._service_session_ids[service_name] = session_id
        self.saved_service_session_ids.append((service_name, session_id))

    def record_successful_provider_session_metadata(
        self,
        service_name: str,
        provider_session_id: str | None,
    ) -> None:
        self.recorded_success_metadata.append((service_name, provider_session_id))

    def service_session_metadata(self, service_name: str) -> dict[str, str] | None:
        del service_name
        return None

    def exact_transcript_service_name(self) -> str | None:
        return None


class _RuntimeSessionIdentityStoreStandIn:
    def __init__(
        self,
        *,
        service_session_ids: dict[str, str] | None = None,
        service_metadata: dict[str, dict[str, str]] | None = None,
        exact_transcript_service_name: str | None = None,
    ) -> None:
        self._service_session_ids = dict(service_session_ids or {})
        self._service_metadata = dict(service_metadata or {})
        self._exact_transcript_service_name = exact_transcript_service_name
        self.saved_service_session_ids: list[tuple[str, str]] = []

    def session_uuid(self) -> str:
        return "runtime-session-uuid"

    def service_session_id(self, service_name: str) -> str | None:
        return self._service_session_ids.get(service_name)

    def save_service_session_id(self, service_name: str, session_id: str) -> None:
        self._service_session_ids[service_name] = session_id
        self.saved_service_session_ids.append((service_name, session_id))

    def service_session_metadata(
        self,
        service_name: str,
    ) -> dict[str, str] | None:
        return self._service_metadata.get(service_name)

    def exact_transcript_service_name(self) -> str | None:
        return self._exact_transcript_service_name


class _RuntimePathOnlyIdentityStoreStandIn:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.saved_service_session_ids: list[tuple[str, str]] = []

    def save_service_session_id(self, service_name: str, session_id: str) -> None:
        save_service_session_id(self.path, service_name, session_id)
        self.saved_service_session_ids.append((service_name, session_id))

    def service_session_metadata(
        self,
        service_name: str,
    ) -> dict[str, str] | None:
        return load_service_session_metadata(self.path, service_name)

    def exact_transcript_service_name(self) -> str | None:
        return None


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


class _CredentialFailureRuntimeService(_PlanRecordingRuntimeService):
    def __init__(
        self,
        name: str,
        provider_state: ProviderSessionState,
        *,
        observed_provider_session_id: str,
        status_code: int,
        provider_service_name: str,
    ) -> None:
        super().__init__(name, provider_state)
        self._observed_provider_session_id = observed_provider_session_id
        self._status_code = status_code
        self._provider_service_name = provider_service_name
        self.mark_exhausted_calls: list[datetime | None] = []

    def run(
        self,
        lines: Iterable[str],
        on_provider_session_id: Callable[[str], None] | None = None,
    ) -> Iterator[ParsedTurn]:
        list(lines)
        if on_provider_session_id is not None:
            on_provider_session_id(self._observed_provider_session_id)
        yield CredentialFailure(
            status_code=self._status_code,
            raw_message="credential failure from provider adapter",
            service_name=self._provider_service_name,
            classification="operator_actionable_credential_failure",
            source_observations=(),
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


class _RuntimeWorkRunnerStandIn:
    def __init__(self, result: str = "adapter result") -> None:
        self._result = result
        self.work_text_calls: list[
            tuple[AgentRole, object, RunKind, str | None, str]
        ] = []

    async def setup(self, git_name: str, git_email: str, work_body: str = "") -> None:
        del git_name, git_email, work_body

    async def work(
        self,
        role: AgentRole,
        prompt: str,
        *,
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
        on_provider_session_id: Callable[[str], None] | None = None,
    ) -> AgentOutput:
        del role, prompt, run_kind, session_uuid, on_provider_session_id
        raise AssertionError("runtime text invocation should use work_text")

    async def work_text(
        self,
        prompt: str,
        *,
        role: AgentRole = AgentRole.IMPLEMENTER,
        tool_policy: object = "full",
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
        on_provider_session_id: Callable[[str], None] | None = None,
    ) -> str:
        del on_provider_session_id
        self.work_text_calls.append((role, tool_policy, run_kind, session_uuid, prompt))
        return self._result


class _PreparedProviderRunSessionStandIn:
    def __init__(self) -> None:
        self.run_kind = RunKind.FRESH
        self.provider_session_id: str | None = None
        self.recorded_provider_session_ids: list[str] = []
        self.successful_run_calls = 0

    def record_provider_session_id(self, provider_session_id: str) -> None:
        self.recorded_provider_session_ids.append(provider_session_id)
        self.provider_session_id = provider_session_id

    def record_successful_run(self) -> None:
        self.successful_run_calls += 1


class _PreparedRuntimeSessionStandIn:
    def __init__(self) -> None:
        self.provider_state_dir_container_path: str | None = None
        self.initial_session = _PreparedProviderRunSessionStandIn()

    def prepare_for_run(self) -> None:
        return None

    def initial_provider_run_session(self) -> _PreparedProviderRunSessionStandIn:
        return self.initial_session

    def resumable_provider_run_session(self) -> _PreparedProviderRunSessionStandIn:
        raise AssertionError("runtime prompt test should not require a resumable run")

    def protocol_reprompt_provider_run_session(self) -> None:
        return None


class _RecordingStatusDisplay:
    def __init__(self) -> None:
        self.remove_calls: list[tuple[str, str, str]] = []

    def register(
        self,
        caller: str,
        kind: str,
        startup_message: str = "started",
        work_body: str = "",
        initial_phase: str = "Setup",
        color_key: int | None = None,
        model_display=None,
    ) -> None:
        del (
            caller,
            kind,
            startup_message,
            work_body,
            initial_phase,
            color_key,
            model_display,
        )

    def update_phase(self, name: str, phase: str) -> None:
        del name, phase

    def reset_idle_timer(self, name: str) -> None:
        del name

    def update_tokens(self, name: str, current_tokens: int) -> None:
        del name, current_tokens

    def remove(
        self,
        caller: str,
        shutdown_message: str = "finished",
        shutdown_style: str = "success",
    ) -> None:
        self.remove_calls.append((caller, shutdown_message, shutdown_style))

    def print(self, caller: str, message: object, style: str | None = None) -> None:
        del caller, message, style


class _PromptRuntimeExecutionAdapterStandIn:
    def __init__(
        self,
        *,
        git_service: GitService,
        service: _RecordingRuntimeService,
        session: _RuntimeSessionStandIn,
        runner: _RuntimeWorkRunnerStandIn | None = None,
    ) -> None:
        self._git_service = git_service
        self._service = service
        self._session = session
        self.work_runner = runner or _RuntimeWorkRunnerStandIn()
        self.resolve_service_calls: list[str] = []
        self.build_work_dependency_calls: list[tuple[str, str, str, object]] = []
        self.prepare_session_calls: list[RunSessionPlan] = []

    def resolve_service(self, service_name: str = "") -> _RecordingRuntimeService:
        self.resolve_service_calls.append(service_name)
        assert service_name == self._service.name
        return self._service

    def build_work_dependencies(
        self,
        *,
        name: str,
        model: str,
        effort: str,
        service: _RecordingRuntimeService,
    ) -> WorkInvocationDependencies:
        self.build_work_dependency_calls.append((name, model, effort, service))

        def _build_runner(*_args: object) -> WorkExecutionAdapter:
            return self.work_runner

        def _prepare_session(
            run_session_plan: RunSessionPlan,
        ) -> _PreparedRuntimeSessionStandIn:
            self.prepare_session_calls.append(run_session_plan)
            return _PreparedRuntimeSessionStandIn()

        return WorkInvocationDependencies(
            container_workspace="/home/agent/workspace",
            timeout_retries=0,
            stage_key_for_role=lambda role: role.value,
            build_session=lambda *_args: self._session,
            build_runner=_build_runner,
            get_git_identity=lambda: (
                self._git_service.get_user_name(),
                self._git_service.get_user_email(),
            ),
            prepare_session=_prepare_session,
        )


def test_runtime_package_runs_prompt_contract_and_returns_llm_output(tmp_path: Path):
    runtime = _runtime_namespace()

    managed_mount = _managed_mount(tmp_path)
    service = _RecordingRuntimeService("codex")
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client([]),
        service_registry={"codex": service},
    )
    registry = runtime.ServiceRegistry({"codex": service})
    request = runtime.PromptRunRequest(
        name="Runtime Consumer",
        worktree=runtime.WorktreeMount(managed_mount),
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
    assert [getattr(policy, "value", None) for policy in service.tool_policies] == [
        runtime.ToolPolicy.PARTIAL.value
    ]


def test_runtime_package_text_output_reducer_returns_result_text() -> None:
    turns: list[str] = []

    result = reduce_text_output_events(
        (
            AssistantTurn(text="first turn"),
            Result(text="exact text from result"),
            AssistantTurn(text="ignored after result"),
        ),
        turns.append,
        provider="codex",
    )

    assert result == "exact text from result"
    assert turns == ["first turn"]


def test_runtime_parsed_event_reducer_success_surface_prefers_result_and_stops_consuming() -> (
    None
):
    turns: list[str] = []
    consumed_events: list[str] = []

    def tracking_events() -> Iterator[ParsedTurn]:
        for label, event in (
            ("first", AssistantTurn(text="first turn")),
            ("result", Result(text="exact text from result")),
            ("later", AssistantTurn(text="ignored after result")),
        ):
            consumed_events.append(label)
            yield event

    result = parsed_event_reducer.reduce_successful_text_output_events(
        tracking_events(),
        turns.append,
    )

    assert result == "exact text from result"
    assert turns == ["first turn"]
    assert consumed_events == ["first", "result"]


def test_runtime_package_text_output_reducer_returns_joined_assistant_turns() -> None:
    turns: list[str] = []

    result = reduce_text_output_events(
        (
            AssistantTurn(text="first turn"),
            AssistantTurn(text="second turn"),
        ),
        turns.append,
        provider="codex",
    )

    assert result == "first turn\nsecond turn"
    assert turns == ["first turn", "second turn"]


def test_runtime_parsed_event_reducer_success_surface_joins_assistant_turns() -> None:
    turns: list[str] = []

    result = parsed_event_reducer.reduce_successful_text_output_events(
        (
            AssistantTurn(text="first turn"),
            AssistantTurn(text="second turn"),
        ),
        turns.append,
    )

    assert result == "first turn\nsecond turn"
    assert turns == ["first turn", "second turn"]


def test_runtime_parsed_event_reducer_protocol_surface_returns_early_output_and_stops_consuming() -> (
    None
):
    turns: list[str] = []
    consumed_events: list[str] = []

    def tracking_events() -> Iterator[ParsedTurn]:
        for label, event in (
            ("first", AssistantTurn(text="working")),
            ("complete", AssistantTurn(text="<promise>COMPLETE</promise>")),
            ("later", Result(text="ignored final result")),
        ):
            consumed_events.append(label)
            yield event

    result = parsed_event_reducer.reduce_successful_text_output_events(
        tracking_events(),
        turns.append,
        extract_early_output=lambda turn: (
            CompletionOutput() if "<promise>COMPLETE</promise>" in turn else None
        ),
        extract_final_output=lambda text: CompletionOutput(issue_numbers=(len(text),)),
    )

    assert result == CompletionOutput()
    assert turns == ["working", "<promise>COMPLETE</promise>"]
    assert consumed_events == ["first", "complete"]


def test_runtime_parsed_event_reducer_protocol_surface_passes_result_text_to_final_extractor() -> (
    None
):
    seen_final_text: list[str] = []

    def extract_final_output(text: str) -> CompletionOutput:
        seen_final_text.append(text)
        return CompletionOutput(issue_numbers=(len(text),))

    result = parsed_event_reducer.reduce_successful_text_output_events(
        (
            AssistantTurn(text="working"),
            Result(text="final result envelope"),
        ),
        lambda _turn: None,
        extract_early_output=lambda _turn: None,
        extract_final_output=extract_final_output,
    )

    assert result == CompletionOutput(issue_numbers=(21,))
    assert seen_final_text == ["final result envelope"]


def test_runtime_parsed_event_reducer_protocol_surface_passes_joined_transcript_to_final_extractor_without_result() -> (
    None
):
    seen_final_text: list[str] = []

    def extract_final_output(text: str) -> CompletionOutput:
        seen_final_text.append(text)
        return CompletionOutput(issue_numbers=(len(text),))

    result = parsed_event_reducer.reduce_successful_text_output_events(
        (
            AssistantTurn(text="first turn"),
            AssistantTurn(text="second turn"),
        ),
        lambda _turn: None,
        extract_early_output=lambda _turn: None,
        extract_final_output=extract_final_output,
    )

    assert result == CompletionOutput(issue_numbers=(22,))
    assert seen_final_text == ["first turn\nsecond turn"]


def test_runtime_parsed_event_reducer_protocol_surface_passes_consumed_transcript_to_post_processing() -> (
    None
):
    seen_post_process: list[tuple[CompletionOutput, str]] = []

    def post_process_output(
        output: CompletionOutput, transcript: str
    ) -> CompletionOutput:
        seen_post_process.append((output, transcript))
        return CompletionOutput(issue_numbers=(len(transcript),))

    result = parsed_event_reducer.reduce_successful_text_output_events(
        (
            AssistantTurn(text="first turn"),
            AssistantTurn(text="second turn"),
            Result(text="final result envelope"),
        ),
        lambda _turn: None,
        extract_early_output=lambda _turn: None,
        extract_final_output=lambda _text: CompletionOutput(),
        post_process_output=post_process_output,
    )

    assert result == CompletionOutput(issue_numbers=(22,))
    assert seen_post_process == [(CompletionOutput(), "first turn\nsecond turn")]


def test_runtime_package_text_output_reducer_updates_prompt_tokens_and_ignores_unsupported_tokens() -> (
    None
):
    turns: list[str] = []
    token_counts: list[int] = []

    result = reduce_text_output_events(
        (
            PromptTokens(count=42_000),
            UnsupportedTokens(count=99, source="codex.turn.completed.usage"),
            AssistantTurn(text="assistant turn"),
        ),
        turns.append,
        token_counts.append,
        provider="codex",
    )

    assert result == "assistant turn"
    assert turns == ["assistant turn"]
    assert token_counts == [42_000]


def test_runtime_parsed_event_reducer_success_surface_updates_prompt_tokens_and_ignores_unsupported_tokens() -> (
    None
):
    turns: list[str] = []
    token_counts: list[int] = []

    result = parsed_event_reducer.reduce_successful_text_output_events(
        (
            PromptTokens(count=42_000),
            UnsupportedTokens(count=99, source="codex.turn.completed.usage"),
            AssistantTurn(text="assistant turn"),
            Result(text="exact text from result"),
        ),
        turns.append,
        token_counts.append,
    )

    assert result == "exact text from result"
    assert turns == ["assistant turn"]
    assert token_counts == [42_000]


def test_runtime_parsed_event_reducer_success_surface_returns_empty_string_for_no_events() -> (
    None
):
    result = parsed_event_reducer.reduce_successful_text_output_events(
        (),
        lambda _turn: None,
    )

    assert result == ""


def test_runtime_parsed_event_reducer_failure_surface_raises_usage_limit_error() -> (
    None
):
    reset_time = datetime(2026, 6, 19, 12, 0, 0)

    with pytest.raises(UsageLimitError) as excinfo:
        parsed_event_reducer.reduce_text_output_events(
            (
                UsageLimit(
                    reset_time=reset_time,
                    raw_message="provider rate limit exceeded",
                    is_permanent=True,
                ),
            ),
            lambda _turn: None,
            provider="codex",
        )

    assert excinfo.value.reset_time == reset_time
    assert excinfo.value.raw_message == "provider rate limit exceeded"
    assert excinfo.value.is_permanent is True
    assert excinfo.value.provider == "codex"


def test_runtime_parsed_event_reducer_failure_surface_raises_usage_limit_after_success_events() -> (
    None
):
    turns: list[str] = []
    token_counts: list[int] = []

    with pytest.raises(UsageLimitError) as excinfo:
        parsed_event_reducer.reduce_text_output_events(
            (
                PromptTokens(count=42_000),
                AssistantTurn(text="first turn"),
                UsageLimit(reset_time=None, raw_message="rate limited"),
            ),
            turns.append,
            token_counts.append,
            provider="codex",
        )

    assert turns == ["first turn"]
    assert token_counts == [42_000]
    assert excinfo.value.raw_message == "rate limited"
    assert excinfo.value.provider == "codex"


def test_runtime_parsed_event_reducer_failure_surface_raises_usage_limit_before_final_extraction() -> (
    None
):
    final_called = False
    post_process_called = False

    def extract_final_output(text: str) -> CompletionOutput:
        nonlocal final_called
        final_called = True
        return CompletionOutput(issue_numbers=(len(text),))

    def post_process_output(
        output: CompletionOutput, transcript: str
    ) -> CompletionOutput:
        nonlocal post_process_called
        post_process_called = True
        return CompletionOutput(issue_numbers=(len(transcript),))

    with pytest.raises(UsageLimitError) as excinfo:
        parsed_event_reducer.reduce_text_output_events(
            (
                PromptTokens(count=42_000),
                AssistantTurn(text="first turn"),
                UsageLimit(reset_time=None, raw_message="rate limited"),
            ),
            lambda _turn: None,
            provider="codex",
            extract_early_output=lambda _turn: None,
            extract_final_output=extract_final_output,
            post_process_output=post_process_output,
        )

    assert excinfo.value.raw_message == "rate limited"
    assert excinfo.value.provider == "codex"
    assert final_called is False
    assert post_process_called is False


def test_runtime_parsed_event_reducer_failure_surface_raises_transient_agent_error() -> (
    None
):
    with pytest.raises(TransientAgentError) as excinfo:
        parsed_event_reducer.reduce_text_output_events(
            (
                TransientError(
                    status_code=529,
                    raw_message="provider overloaded",
                ),
            ),
            lambda _turn: None,
            provider="codex",
        )

    assert str(excinfo.value) == "provider overloaded"
    assert excinfo.value.status_code == 529


def test_runtime_parsed_event_reducer_failure_surface_raises_hard_agent_error() -> None:

    with pytest.raises(HardAgentError) as excinfo:
        parsed_event_reducer.reduce_text_output_events(
            (
                HardError(
                    status_code=403,
                    raw_message="provider rejected request",
                    classification="permission_denied",
                ),
            ),
            lambda _turn: None,
            provider="codex",
        )

    assert str(excinfo.value) == "provider rejected request"
    assert excinfo.value.status_code == 403
    assert excinfo.value.classification == "permission_denied"
    assert excinfo.value.service_name == "codex"


def test_runtime_parsed_event_reducer_failure_surface_keeps_empty_provider_identity() -> (
    None
):
    with pytest.raises(HardAgentError) as excinfo:
        parsed_event_reducer.reduce_text_output_events(
            (
                HardError(
                    status_code=400,
                    raw_message="provider rejected request",
                ),
            ),
            lambda _turn: None,
            provider="",
        )

    assert excinfo.value.service_name == ""


def test_runtime_parsed_event_reducer_failure_surface_raises_credential_failure() -> (
    None
):

    with pytest.raises(AgentCredentialFailureError) as excinfo:
        parsed_event_reducer.reduce_text_output_events(
            (
                CredentialFailure(
                    raw_message="Codex authentication missing",
                    service_name="codex",
                    classification="operator_actionable_credential_failure",
                    source_observations=(
                        ("json_event.error", "Codex authentication missing"),
                    ),
                    status_code=401,
                ),
            ),
            lambda _turn: None,
            provider="claude",
        )

    assert str(excinfo.value) == "Codex authentication missing"
    assert excinfo.value.status_code == 401
    assert excinfo.value.classification == "operator_actionable_credential_failure"
    assert excinfo.value.service_name == "codex"


def test_runtime_parsed_event_reducer_failure_surface_raises_transient_error_after_success_events() -> (
    None
):
    turns: list[str] = []
    token_counts: list[int] = []

    with pytest.raises(TransientAgentError) as excinfo:
        parsed_event_reducer.reduce_text_output_events(
            (
                PromptTokens(count=42_000),
                AssistantTurn(text="first turn"),
                TransientError(
                    status_code=529,
                    raw_message="API Error: 529 Overloaded",
                ),
            ),
            turns.append,
            token_counts.append,
            provider="codex",
        )

    assert turns == ["first turn"]
    assert token_counts == [42_000]
    assert str(excinfo.value) == "API Error: 529 Overloaded"
    assert excinfo.value.status_code == 529


def test_runtime_parsed_event_reducer_failure_surface_raises_hard_error_after_success_events() -> (
    None
):
    turns: list[str] = []
    token_counts: list[int] = []

    with pytest.raises(HardAgentError) as excinfo:
        parsed_event_reducer.reduce_text_output_events(
            (
                PromptTokens(count=42_000),
                AssistantTurn(text="first turn"),
                HardError(
                    status_code=403,
                    raw_message="API Error: 403 Forbidden",
                    classification="permission_denied",
                ),
            ),
            turns.append,
            token_counts.append,
            provider="codex",
        )

    assert turns == ["first turn"]
    assert token_counts == [42_000]
    assert str(excinfo.value) == "API Error: 403 Forbidden"
    assert excinfo.value.status_code == 403
    assert excinfo.value.service_name == "codex"
    assert excinfo.value.classification == "permission_denied"


def test_runtime_parsed_event_reducer_failure_surface_raises_credential_failure_after_success_events() -> (
    None
):
    turns: list[str] = []
    token_counts: list[int] = []

    with pytest.raises(AgentCredentialFailureError) as excinfo:
        parsed_event_reducer.reduce_text_output_events(
            (
                PromptTokens(count=42_000),
                AssistantTurn(text="first turn"),
                CredentialFailure(
                    raw_message="credential failure from provider adapter",
                    service_name="codex",
                    classification="operator_actionable_credential_failure",
                    source_observations=(),
                    status_code=401,
                ),
            ),
            turns.append,
            token_counts.append,
            provider="claude",
        )

    assert turns == ["first turn"]
    assert token_counts == [42_000]
    assert str(excinfo.value) == "credential failure from provider adapter"
    assert excinfo.value.status_code == 401
    assert excinfo.value.service_name == "codex"
    assert excinfo.value.classification == "operator_actionable_credential_failure"


def test_runtime_agent_log_lifecycle_runs_standalone_without_pycastle(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]

    result = _standalone_runtime_agent_log_result(repo_root, tmp_path / "runtime-logs")

    assert Path(cast(str, result["log_path"])) == tmp_path / "runtime-logs" / (
        "standalone-runtime-20260517T1430.log"
    )
    assert result["log_lines"] == [
        (
            '{"type": "agent_invocation", "role": "implementer", '
            '"run_kind": "fresh", "provider_session_id": "provider-session-123", '
            '"prompt": "already rendered prompt"}'
        ),
        '{"type":"result","result":"done"}',
    ]


@pytest.mark.parametrize(
    ("tz_name", "expected_filename"),
    [
        ("UTC", "standalone-runtime-20260517T1430.log"),
        ("Etc/GMT+7", "standalone-runtime-20260517T0730.log"),
        ("Etc/GMT-14", "standalone-runtime-20260518T0430.log"),
    ],
)
def test_runtime_agent_log_lifecycle_uses_local_minute_timestamp_standalone(
    tmp_path: Path,
    tz_name: str,
    expected_filename: str,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]

    result = _standalone_runtime_agent_log_result_with_timezone(
        repo_root,
        tmp_path / tz_name.replace("/", "-"),
        tz_name=tz_name,
    )

    assert Path(cast(str, result["log_path"])).name == expected_filename


def test_runtime_package_prompt_entrypoint_requires_build_work_dependencies_adapter(
    tmp_path: Path,
) -> None:
    runtime = _runtime_namespace()

    service = _RecordingRuntimeService("codex")
    registry = runtime.ServiceRegistry({"codex": service})
    request = runtime.PromptRunRequest(
        worktree=runtime.WorktreeMount(tmp_path),
        prompt="Return the final answer only.",
        override=runtime.StageOverride(
            service="codex",
            model="gpt-5.4",
            effort="medium",
        ),
    )

    class _MissingBuildWorkDependenciesAdapter:
        def resolve_service(self, service_name: str = "") -> _RecordingRuntimeService:
            assert service_name == "codex"
            return service

    with pytest.raises(
        RuntimeConfigurationError,
        match=r"execution adapter with callable `build_work_dependencies\(\)`",
    ):
        asyncio.run(
            runtime.run_prompt(
                runner=_MissingBuildWorkDependenciesAdapter(),
                service_registry=registry,
                request=request,
            )
        )


def test_runtime_public_errors_do_not_default_missing_service_names_to_claude():
    from pycastle.errors import AgentFailedError, HardAgentError

    hard_error = HardAgentError(message="provider rejected request", status_code=400)
    failed_error = AgentFailedError(
        role_value="implementer",
        worktree_path=Path("."),
    )

    assert hard_error.service_name == ""
    assert failed_error.service_name == ""
    assert str(failed_error.session_store) == ".pycastle-session/implementer"


def test_runtime_provider_state_relpath_normalizes_legacy_namespaced_layout(
    tmp_path: Path,
) -> None:
    from pycastle.agents.output_protocol import AgentRole as RuntimeAgentRole
    from pycastle.runtime_session import ProviderSessionState, RunKind
    from pycastle.session_planning import (
        ProviderRunStatePlanRequest,
        plan_provider_run_state,
    )

    service = _PlanRecordingRuntimeService(
        "codex",
        ProviderSessionState(RunKind.FRESH, None),
    )
    legacy_relpath = ".pycastle-session/implementer/codex/"
    role_session = _RuntimeRoleSessionStandIn(
        _RuntimeServiceSessionState(
            state_dir=tmp_path / ".pycastle-session" / "implementer" / "codex",
            has_resumable_provider_state=False,
            state_dir_relpath=legacy_relpath,
        )
    )

    plan = plan_provider_run_state(
        ProviderRunStatePlanRequest(
            worktree=tmp_path,
            role=RuntimeAgentRole.IMPLEMENTER,
            namespace="main",
            service=service,
            role_session=role_session,
            provider_session_adapter=_ServiceBackedRuntimeProviderSessionAdapter(
                service
            ),
        )
    )

    assert (
        plan.provider_state_dir_relpath == ".pycastle-session/implementer/main/codex/"
    )
    assert plan.provider_state_dir == (
        tmp_path / ".pycastle-session" / "implementer" / "main" / "codex"
    )


def test_runtime_session_helpers_use_caller_supplied_session_root_and_provider_path(
    tmp_path: Path,
):
    from pycastle.errors import AgentFailedError
    from pycastle.agents.output_protocol import AgentRole as RuntimeAgentRole
    from pycastle.runtime_session import (
        normalize_state_dir_relpath,
        provider_state_relpath,
    )

    session_root = ".runtime-session"

    assert (
        provider_state_relpath(
            RuntimeAgentRole.IMPLEMENTER,
            "codex",
            "main",
            session_root=session_root,
        )
        == ".runtime-session/implementer/main/codex/"
    )
    assert (
        normalize_state_dir_relpath(
            RuntimeAgentRole.IMPLEMENTER,
            "main",
            "codex",
            ".runtime-session/implementer/codex/",
            session_root=session_root,
        )
        == ".runtime-session/implementer/main/codex/"
    )

    failure = AgentFailedError(
        role_value="reviewer",
        worktree_path=tmp_path,
        namespace="main",
        failure_class="protocol_error",
        service_name="codex",
    )

    assert str(failure.session_store) == ".pycastle-session/reviewer/main/codex"


def test_runtime_session_helpers_default_to_provider_neutral_relpaths():
    from pycastle.agents.output_protocol import AgentRole as RuntimeAgentRole
    from pycastle.runtime_session import (
        normalize_state_dir_relpath,
        provider_state_relpath,
    )

    assert (
        provider_state_relpath(
            RuntimeAgentRole.IMPLEMENTER,
            "codex",
            "main",
        )
        == "implementer/main/codex/"
    )
    assert (
        normalize_state_dir_relpath(
            RuntimeAgentRole.IMPLEMENTER,
            "main",
            "codex",
            "implementer/codex/",
        )
        == "implementer/main/codex/"
    )


def test_runtime_provider_state_plan_records_observed_provider_session_id_for_opencode(
    tmp_path: Path,
) -> None:
    from pycastle.runtime_session import RunKind
    from pycastle.session_planning import (
        AuthSeedingRequirement,
        ProviderRunStatePlan,
        RecoveredSessionIdPersistence,
        record_observed_provider_session_id,
    )

    service_state_dir = tmp_path / ".pycastle-session" / "implementer" / "opencode"
    role_session = _RuntimeRoleSessionStandIn(
        _RuntimeServiceSessionState(
            state_dir=service_state_dir,
            has_resumable_provider_state=False,
            state_dir_relpath=".pycastle-session/implementer/opencode/",
        )
    )
    plan = ProviderRunStatePlan(
        role_session=role_session,
        provider_session_adapter=_RecordingProviderSessionAdapter(
            ProviderSessionState(RunKind.FRESH, None),
            service_name="opencode",
        ),
        service_name="opencode",
        run_kind=RunKind.FRESH,
        provider_state_dir=service_state_dir,
        provider_state_dir_relpath=".pycastle-session/implementer/opencode/",
        provider_session_id=None,
        auth_seeding_requirement=AuthSeedingRequirement.NOT_REQUIRED,
        recovered_session_id_persistence=RecoveredSessionIdPersistence.SKIP,
        service_state_dir=service_state_dir,
    )

    record_observed_provider_session_id(
        provider_run_state_plan=plan,
        provider_session_id="sess-runtime-opencode",
    )

    assert role_session.saved_service_session_ids == [
        ("opencode", "sess-runtime-opencode")
    ]
    assert (service_state_dir / "session_id").exists() is False


def test_runtime_session_helpers_recover_and_persist_opencode_session_id(
    tmp_path: Path,
) -> None:
    from pycastle.runtime_session import (
        load_state_dir_provider_session_id,
        provider_state_session_id_path,
        select_resumable_provider_session_id,
    )

    state_dir = tmp_path / ".pycastle-session" / "implementer" / "opencode"
    state_dir.mkdir(parents=True)
    provider_state_session_id_path(state_dir, "opencode").write_text(
        "sess-runtime-opencode\n",
        encoding="utf-8",
    )
    role_session = _RuntimeRoleSessionStandIn(
        _RuntimeServiceSessionState(
            state_dir=state_dir,
            has_resumable_provider_state=True,
            state_dir_relpath=".pycastle-session/implementer/opencode/",
        )
    )

    selection = select_resumable_provider_session_id(
        role_session,
        "opencode",
        provider_state_dir=state_dir,
        has_resumable_provider_state=True,
    )

    assert load_state_dir_provider_session_id(state_dir, "opencode") == (
        "sess-runtime-opencode"
    )
    assert selection.provider_session_id == "sess-runtime-opencode"
    assert selection.persist_provider_session_id is True
    assert role_session.saved_service_session_ids == [
        ("opencode", "sess-runtime-opencode")
    ]


def test_runtime_session_helpers_allow_provider_adapter_to_recover_custom_session_id_filename(
    tmp_path: Path,
) -> None:
    from pycastle.runtime_session import (
        load_provider_state_session_id,
        select_resumable_provider_session_id,
    )

    state_dir = tmp_path / ".pycastle-session" / "implementer" / "opencode"
    state_dir.mkdir(parents=True)
    (state_dir / "session_id").write_text(
        "sess-runtime-opencode\n",
        encoding="utf-8",
    )
    role_session = _RuntimeRoleSessionStandIn(
        _RuntimeServiceSessionState(
            state_dir=state_dir,
            has_resumable_provider_state=True,
            state_dir_relpath=".pycastle-session/implementer/opencode/",
        )
    )

    selection = select_resumable_provider_session_id(
        role_session,
        "opencode",
        provider_state_dir=state_dir,
        has_resumable_provider_state=True,
        recover_provider_session_id=lambda recovered_state_dir: (
            None
            if recovered_state_dir is None
            else load_provider_state_session_id(recovered_state_dir / "session_id")
        ),
    )

    assert selection.provider_session_id == "sess-runtime-opencode"
    assert selection.persist_provider_session_id is True
    assert role_session.saved_service_session_ids == [
        ("opencode", "sess-runtime-opencode")
    ]


def test_runtime_provider_state_plan_records_successful_run_metadata_through_role_session_interface() -> (
    None
):
    from pycastle.runtime_session import RunKind
    from pycastle.session_planning import (
        AuthSeedingRequirement,
        ProviderRunStatePlan,
        RecoveredSessionIdPersistence,
        record_successful_provider_session_metadata,
    )

    role_session = _RuntimeRoleSessionStandIn(
        _RuntimeServiceSessionState(
            state_dir=None,
            has_resumable_provider_state=False,
            state_dir_relpath=None,
        )
    )
    plan = ProviderRunStatePlan(
        role_session=role_session,
        provider_session_adapter=_RecordingProviderSessionAdapter(
            ProviderSessionState(RunKind.FRESH, "thread-runtime"),
            service_name="codex",
        ),
        service_name="codex",
        run_kind=RunKind.FRESH,
        provider_state_dir=None,
        provider_state_dir_relpath=None,
        provider_session_id="thread-runtime",
        auth_seeding_requirement=AuthSeedingRequirement.NOT_REQUIRED,
        recovered_session_id_persistence=RecoveredSessionIdPersistence.SKIP,
    )

    record_successful_provider_session_metadata(
        provider_run_state_plan=plan,
        provider_session_id="thread-runtime",
    )

    assert role_session.recorded_success_metadata == [("codex", "thread-runtime")]


def test_runtime_provider_state_plan_records_successful_run_metadata_through_role_session_path(
    tmp_path: Path,
) -> None:
    from pycastle.runtime_session import RunKind
    from pycastle.session_planning import (
        AuthSeedingRequirement,
        ProviderRunStatePlan,
        RecoveredSessionIdPersistence,
    )

    role_session_path = tmp_path / ".pycastle-session" / "implementer"
    role_session = _RuntimePathOnlyIdentityStoreStandIn(role_session_path)
    plan = ProviderRunStatePlan(
        role_session=role_session,
        provider_session_adapter=_RecordingProviderSessionAdapter(
            ProviderSessionState(RunKind.FRESH, "thread-runtime"),
            service_name="codex",
        ),
        service_name="codex",
        run_kind=RunKind.FRESH,
        provider_state_dir=None,
        provider_state_dir_relpath=None,
        provider_session_id="thread-runtime",
        auth_seeding_requirement=AuthSeedingRequirement.NOT_REQUIRED,
        recovered_session_id_persistence=RecoveredSessionIdPersistence.SKIP,
    )

    plan.record_successful_run("thread-runtime")

    assert load_service_session_metadata(role_session_path, "codex") == {
        "service": "codex",
        "provider_session_id": "thread-runtime",
    }


def test_runtime_provider_session_adapter_plan_allows_execution_service_without_session_methods(
    tmp_path: Path,
) -> None:
    from pycastle.agents.output_protocol import AgentRole as RuntimeAgentRole
    from pycastle.runtime_session import RunKind
    from pycastle.session_planning import (
        AuthSeedingRequirement,
        ProviderRunStatePlanRequest,
        plan_provider_run_state,
    )

    service_state_dir = (
        tmp_path / ".pycastle-session" / "implementer" / "main" / "generic"
    )
    role_session = _RuntimeRoleSessionStandIn(
        _RuntimeServiceSessionState(
            state_dir=service_state_dir,
            has_resumable_provider_state=True,
            state_dir_relpath=".pycastle-session/implementer/main/generic/",
        )
    )
    adapter = _RecordingProviderSessionAdapter(
        ProviderSessionState(
            RunKind.RESUME,
            "adapter-session-id",
            state_dir_relpath="custom/provider-state/",
            state_dir_path=tmp_path / "custom" / "provider-state",
            exact_transcript_match=True,
            persist_provider_session_id=True,
            auth_seeding_requirement=AuthSeedingRequirement.REQUIRED,
        )
    )

    plan = plan_provider_run_state(
        ProviderRunStatePlanRequest(
            worktree=tmp_path,
            role=RuntimeAgentRole.IMPLEMENTER,
            namespace="main",
            service=cast(Any, _ExecutionOnlyRuntimeService("generic")),
            role_session=role_session,
            provider_session_adapter=adapter,
        )
    )

    assert len(adapter.preferences_requests) == 1
    assert adapter.preferences_requests[0].provider_state_dir == service_state_dir
    assert adapter.preferences_requests[0].has_resumable_provider_state is True
    assert adapter.preferences_requests[0].state_dir_relpath == (
        ".pycastle-session/implementer/main/generic/"
    )
    assert len(adapter.state_requests) == 1
    assert adapter.state_requests[0].preferred_provider_session_id == (
        "preferred-session-id"
    )
    assert adapter.state_requests[0].require_exact_transcript_match is True
    assert plan.run_kind is RunKind.RESUME
    assert plan.provider_session_id == "adapter-session-id"
    assert plan.provider_state_dir == tmp_path / "custom" / "provider-state"
    assert plan.provider_state_dir_relpath == "custom/provider-state/"
    assert plan.exact_transcript_match is True
    assert plan.auth_seeding_requirement is AuthSeedingRequirement.REQUIRED


def test_runtime_provider_session_adapter_planning_facts_supply_provider_state_dir(
    tmp_path: Path,
) -> None:
    from pycastle.agents.output_protocol import AgentRole as RuntimeAgentRole
    from pycastle.runtime_session import ProviderSessionState, RunKind
    from pycastle.session_planning import (
        ProviderRunStatePlanRequest,
        plan_provider_run_state,
    )

    adapter_provider_state_dir = tmp_path / "adapter-state" / "generic"
    role_session = _RuntimeRoleSessionStandIn(
        _RuntimeServiceSessionState(
            state_dir=tmp_path
            / ".pycastle-session"
            / "implementer"
            / "main"
            / "generic",
            has_resumable_provider_state=True,
            state_dir_relpath=".pycastle-session/implementer/main/generic/",
        )
    )
    adapter = _RecordingProviderSessionAdapter(
        ProviderSessionState(RunKind.RESUME, "adapter-session-id")
    )
    adapter.set_planning_facts_provider_state_dir(adapter_provider_state_dir)

    plan = plan_provider_run_state(
        ProviderRunStatePlanRequest(
            worktree=tmp_path,
            role=RuntimeAgentRole.IMPLEMENTER,
            namespace="main",
            service=cast(Any, _ExecutionOnlyRuntimeService("generic")),
            role_session=role_session,
            provider_session_adapter=adapter,
        )
    )

    assert (
        adapter.preferences_requests[0].provider_state_dir == adapter_provider_state_dir
    )
    assert adapter.state_requests[0].provider_state_dir == adapter_provider_state_dir
    assert plan.service_state_dir == adapter_provider_state_dir


def test_runtime_provider_session_adapter_handles_local_preparation_and_session_recording(
    tmp_path: Path,
) -> None:
    from pycastle.agents.output_protocol import AgentRole as RuntimeAgentRole
    from pycastle.runtime_session import RunKind
    from pycastle.session_planning import (
        ProviderRunStatePlanRequest,
        plan_provider_run_state,
    )

    service_state_dir = (
        tmp_path / ".pycastle-session" / "implementer" / "main" / "generic"
    )
    selected_state_dir = tmp_path / "custom" / "provider-state"
    role_session = _RuntimeRoleSessionStandIn(
        _RuntimeServiceSessionState(
            state_dir=service_state_dir,
            has_resumable_provider_state=False,
            state_dir_relpath=".pycastle-session/implementer/main/generic/",
        )
    )
    adapter = _RecordingProviderSessionAdapter(
        ProviderSessionState(
            RunKind.FRESH,
            None,
            state_dir_relpath="custom/provider-state/",
            state_dir_path=selected_state_dir,
        )
    )

    plan = plan_provider_run_state(
        ProviderRunStatePlanRequest(
            worktree=tmp_path,
            role=RuntimeAgentRole.IMPLEMENTER,
            namespace="main",
            service=cast(Any, _ExecutionOnlyRuntimeService("generic")),
            role_session=role_session,
            provider_session_adapter=adapter,
        )
    )

    plan.prepare_provider_state_dir()
    plan.remember_provider_session_id("adapter-recorded-session")

    assert adapter.prepare_calls == [(selected_state_dir, None)]
    assert adapter.record_calls == [
        ("adapter-recorded-session", service_state_dir),
    ]
    assert role_session.saved_service_session_ids == [
        ("generic", "adapter-recorded-session")
    ]


def test_runtime_session_select_resumable_provider_session_id_persists_state_dir_sidecar_identity(
    tmp_path: Path,
) -> None:
    from pycastle.runtime_session import (
        select_resumable_provider_session_id,
    )

    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    state_dir.mkdir(parents=True)
    (state_dir / "thread_id").write_text("thread-from-sidecar\n", encoding="utf-8")
    role_session = _RuntimeSessionIdentityStoreStandIn()

    selection = select_resumable_provider_session_id(
        role_session,
        "codex",
        provider_state_dir=state_dir,
        has_resumable_provider_state=True,
    )

    assert selection.provider_session_id == "thread-from-sidecar"
    assert selection.persist_provider_session_id is True
    assert role_session.saved_service_session_ids == [("codex", "thread-from-sidecar")]


def test_runtime_session_select_resumable_provider_session_id_reads_sidecar_from_path_only_store(
    tmp_path: Path,
) -> None:
    from pycastle.runtime_session import select_resumable_provider_session_id

    role_session_path = tmp_path / ".pycastle-session" / "implementer"
    save_service_session_id(
        role_session_path,
        "codex",
        "thread-from-role-session-sidecar",
    )
    role_session = _RuntimePathOnlyIdentityStoreStandIn(role_session_path)

    selection = select_resumable_provider_session_id(
        role_session,
        "codex",
        provider_state_dir=tmp_path / ".pycastle-session" / "implementer" / "codex",
        has_resumable_provider_state=True,
    )

    assert selection.provider_session_id == "thread-from-role-session-sidecar"
    assert selection.persist_provider_session_id is False
    assert role_session.saved_service_session_ids == []


def test_runtime_session_exact_resume_uses_injected_provider_identity_matcher(
    tmp_path: Path,
) -> None:
    from pycastle.runtime_session import (
        is_exact_resumable_service_session,
    )

    state_dir = tmp_path / ".runtime-session" / "provider"
    state_dir.mkdir(parents=True)
    role_session = _RuntimeSessionIdentityStoreStandIn(
        service_metadata={
            "provider": {
                "service": "provider",
                "provider_session_id": "thread-exact",
            }
        },
        exact_transcript_service_name="provider",
    )

    assert (
        is_exact_resumable_service_session(
            role_session,
            "provider",
            provider_session_id="thread-exact",
            provider_state_dir=state_dir,
            exact_provider_session_matcher=(
                lambda provider_session_id, provider_state_dir: (
                    provider_session_id == "thread-exact"
                    and provider_state_dir == state_dir
                )
            ),
        )
        is True
    )
    assert (
        is_exact_resumable_service_session(
            role_session,
            "provider",
            provider_session_id="thread-other",
            provider_state_dir=state_dir,
            exact_provider_session_matcher=(
                lambda provider_session_id, provider_state_dir: (
                    provider_session_id == "thread-exact"
                    and provider_state_dir == state_dir
                )
            ),
        )
        is False
    )


def test_runtime_provider_state_plan_exposes_codex_auth_seed_action_for_missing_auth_json(
    tmp_path: Path,
) -> None:
    from pycastle.agents.output_protocol import AgentRole as RuntimeAgentRole
    from pycastle.runtime_session import ProviderSessionState, RunKind
    from pycastle.session_planning import (
        AuthSeedingRequirement,
        LocalAuthSeedAction,
        ProviderRunStatePlanRequest,
        plan_provider_run_state,
    )

    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    role_session = _RuntimeRoleSessionStandIn(
        _RuntimeServiceSessionState(
            state_dir=state_dir,
            has_resumable_provider_state=False,
            state_dir_relpath=".pycastle-session/implementer/codex/",
        )
    )
    service = _PlanRecordingRuntimeService(
        "codex",
        ProviderSessionState(
            RunKind.FRESH,
            None,
            auth_seeding_requirement=AuthSeedingRequirement.REQUIRED,
            auth_seed_action=LocalAuthSeedAction(
                source=Path.home() / ".codex" / "auth.json",
                destination=state_dir / "auth.json",
            ),
        ),
    )

    plan = plan_provider_run_state(
        ProviderRunStatePlanRequest(
            worktree=tmp_path,
            role=RuntimeAgentRole.IMPLEMENTER,
            namespace="",
            service=service,
            role_session=role_session,
            provider_session_adapter=_ServiceBackedRuntimeProviderSessionAdapter(
                service
            ),
        )
    )

    assert plan.auth_seeding_requirement is AuthSeedingRequirement.REQUIRED
    assert plan.auth_seed_action is not None
    assert plan.auth_seed_action.source == Path.home() / ".codex" / "auth.json"
    assert plan.auth_seed_action.destination == state_dir / "auth.json"


def test_runtime_resident_session_plan_exposes_provider_metadata_without_persistence_policy(
    tmp_path: Path,
) -> None:
    from pycastle.agents.output_protocol import AgentRole as RuntimeAgentRole
    from pycastle.runtime_session import ProviderSessionState, RunKind
    from pycastle.session_planning import (
        AuthSeedingRequirement,
        LocalAuthSeedAction,
        ResidentSessionPlanRequest,
        plan_resident_session,
    )

    selected_state_dir = tmp_path / "custom" / "generic-state"
    host_auth = tmp_path / "host" / "generic-creds.json"
    host_auth.parent.mkdir(parents=True)
    host_auth.write_text("token", encoding="utf-8")
    auth_seed_action = LocalAuthSeedAction(
        source=host_auth,
        destination=selected_state_dir / "generic-creds.json",
    )
    role_session = _RuntimeRoleSessionStandIn(
        _RuntimeServiceSessionState(
            state_dir=tmp_path
            / ".pycastle-session"
            / "implementer"
            / "main"
            / "generic",
            has_resumable_provider_state=True,
            state_dir_relpath=".pycastle-session/implementer/main/generic/",
        )
    )
    provider_state = ProviderSessionState(
        RunKind.RESUME,
        "persisted-session-id",
        state_dir_relpath="custom/generic-state/",
        state_dir_path=selected_state_dir,
        exact_transcript_match=True,
        persist_provider_session_id=True,
        auth_seeding_requirement=AuthSeedingRequirement.REQUIRED,
        auth_seed_action=auth_seed_action,
    )
    service = _PlanRecordingRuntimeService("generic", provider_state)
    adapter = _RecordingProviderSessionAdapter(provider_state, service_name="generic")

    plan = plan_resident_session(
        ResidentSessionPlanRequest(
            worktree=tmp_path,
            role=RuntimeAgentRole.IMPLEMENTER,
            namespace="main",
            service=service,
            role_session=role_session,
            provider_session_adapter=adapter,
        )
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.service_state_dir == (
        tmp_path / ".pycastle-session" / "implementer" / "main" / "generic"
    )
    assert plan.provider_state_dir_relpath == "custom/generic-state/"
    assert plan.host_provider_state_dir == selected_state_dir
    assert plan.provider_session_id == "persisted-session-id"
    assert plan.auth_seeding_requirement is AuthSeedingRequirement.REQUIRED
    assert plan.auth_seed_action == auth_seed_action
    assert plan.exact_transcript_match is True
    assert not hasattr(plan, "recovered_session_id_persistence")

    plan.prepare_provider_state_dir()
    prepared_session_id = plan.prepared_provider_session_id()
    plan.record_provider_session_id("runtime-session-id")
    plan.record_successful_run()

    assert adapter.prepare_calls == [(selected_state_dir, auth_seed_action)]
    assert prepared_session_id == "persisted-session-id"
    assert role_session.saved_service_session_ids == [
        ("generic", "persisted-session-id"),
        ("generic", "runtime-session-id"),
    ]
    assert role_session.recorded_success_metadata == [("generic", "runtime-session-id")]


def test_runtime_provider_state_plan_preserves_provider_auth_seed_failure_policy(
    tmp_path: Path,
) -> None:
    from pycastle.agents.output_protocol import AgentRole as RuntimeAgentRole
    from pycastle.runtime_session import ProviderSessionState, RunKind
    from pycastle.session_planning import (
        AuthSeedingRequirement,
        LocalAuthSeedAction,
        ProviderRunStatePlanRequest,
        plan_provider_run_state,
    )

    state_dir = tmp_path / ".pycastle-session" / "implementer" / "generic"
    missing = tmp_path / "host" / "generic-creds.json"
    role_session = _RuntimeRoleSessionStandIn(
        _RuntimeServiceSessionState(
            state_dir=state_dir,
            has_resumable_provider_state=False,
            state_dir_relpath=".pycastle-session/implementer/generic/",
        )
    )
    service = _PlanRecordingRuntimeService(
        "generic",
        ProviderSessionState(
            RunKind.FRESH,
            None,
            auth_seeding_requirement=AuthSeedingRequirement.REQUIRED,
            auth_seed_action=LocalAuthSeedAction(
                source=missing,
                destination=state_dir / "generic-creds.json",
                missing_source_message="Generic credentials missing on the host.",
                missing_source_service_name="generic",
                missing_source_status_code=403,
                missing_source_classification=(
                    "operator_actionable_credential_failure"
                ),
            ),
        ),
    )

    plan = plan_provider_run_state(
        ProviderRunStatePlanRequest(
            worktree=tmp_path,
            role=RuntimeAgentRole.IMPLEMENTER,
            namespace="",
            service=service,
            role_session=role_session,
            provider_session_adapter=_ServiceBackedRuntimeProviderSessionAdapter(
                service
            ),
        )
    )

    with pytest.raises(AgentCredentialFailureError) as excinfo:
        plan.prepare_provider_state_dir()

    assert str(excinfo.value) == "Generic credentials missing on the host."
    assert excinfo.value.service_name == "generic"
    assert excinfo.value.status_code == 403
    assert excinfo.value.classification == "operator_actionable_credential_failure"


def test_runtime_provider_state_plan_without_provider_auth_seed_policy_raises_file_not_found(
    tmp_path: Path,
) -> None:
    from pycastle.agents.output_protocol import AgentRole as RuntimeAgentRole
    from pycastle.runtime_session import ProviderSessionState, RunKind
    from pycastle.session_planning import (
        AuthSeedingRequirement,
        LocalAuthSeedAction,
        ProviderRunStatePlanRequest,
        plan_provider_run_state,
    )

    state_dir = tmp_path / ".pycastle-session" / "implementer" / "generic"
    missing = tmp_path / "host" / "generic-creds.json"
    role_session = _RuntimeRoleSessionStandIn(
        _RuntimeServiceSessionState(
            state_dir=state_dir,
            has_resumable_provider_state=False,
            state_dir_relpath=".pycastle-session/implementer/generic/",
        )
    )
    service = _PlanRecordingRuntimeService(
        "generic",
        ProviderSessionState(
            RunKind.FRESH,
            None,
            auth_seeding_requirement=AuthSeedingRequirement.REQUIRED,
            auth_seed_action=LocalAuthSeedAction(
                source=missing,
                destination=state_dir / "generic-creds.json",
            ),
        ),
    )

    plan = plan_provider_run_state(
        ProviderRunStatePlanRequest(
            worktree=tmp_path,
            role=RuntimeAgentRole.IMPLEMENTER,
            namespace="",
            service=service,
            role_session=role_session,
            provider_session_adapter=_ServiceBackedRuntimeProviderSessionAdapter(
                service
            ),
        )
    )

    with pytest.raises(FileNotFoundError) as excinfo:
        plan.prepare_provider_state_dir()

    assert excinfo.value.args == (missing,)


def test_runtime_provider_state_plan_keeps_selected_provider_state_dir_for_opencode_resume_without_container_override_policy(
    tmp_path: Path,
) -> None:
    from pycastle.agents.output_protocol import AgentRole as RuntimeAgentRole
    from pycastle.runtime_session import ProviderSessionState, RunKind
    from pycastle.session_planning import (
        ProviderRunStatePlanRequest,
        plan_provider_run_state,
    )

    selected_state_dir = tmp_path / "custom" / "opencode-state"
    service_state_dir = tmp_path / ".pycastle-session" / "implementer" / "opencode"
    role_session = _RuntimeRoleSessionStandIn(
        _RuntimeServiceSessionState(
            state_dir=service_state_dir,
            has_resumable_provider_state=True,
            state_dir_relpath=".pycastle-session/implementer/opencode/",
        )
    )
    service = _PlanRecordingRuntimeService(
        "opencode",
        ProviderSessionState(
            RunKind.RESUME,
            "sess-opencode",
            state_dir_relpath="custom/opencode-state/",
            state_dir_path=selected_state_dir,
        ),
    )

    plan = plan_provider_run_state(
        ProviderRunStatePlanRequest(
            worktree=tmp_path,
            role=RuntimeAgentRole.IMPLEMENTER,
            namespace="",
            service=service,
            role_session=role_session,
            provider_session_adapter=_ServiceBackedRuntimeProviderSessionAdapter(
                service
            ),
        )
    )

    assert plan.provider_state_dir == selected_state_dir
    assert plan.service_state_dir == service_state_dir
    assert plan.provider_state_dir_container_path(
        worktree=tmp_path,
        container_workspace="/workspace",
    ) == ("/workspace/custom/opencode-state/")


def test_runtime_provider_state_plan_uses_service_state_dir_when_provider_requests_container_override_policy(
    tmp_path: Path,
) -> None:
    from pycastle.agents.output_protocol import AgentRole as RuntimeAgentRole
    from pycastle.runtime_session import ProviderSessionState, RunKind
    from pycastle.session_planning import (
        ProviderRunStatePlanRequest,
        plan_provider_run_state,
    )

    service_state_dir = tmp_path / ".pycastle-session" / "implementer" / "generic"
    role_session = _RuntimeRoleSessionStandIn(
        _RuntimeServiceSessionState(
            state_dir=service_state_dir,
            has_resumable_provider_state=True,
            state_dir_relpath=".pycastle-session/implementer/generic/",
        )
    )
    service = _PlanRecordingRuntimeService(
        "generic",
        ProviderSessionState(
            RunKind.RESUME,
            "sess-generic",
            state_dir_relpath="custom/generic-state/",
            state_dir_path=tmp_path / "custom" / "generic-state",
            use_service_state_dir_for_container=True,
        ),
    )

    plan = plan_provider_run_state(
        ProviderRunStatePlanRequest(
            worktree=tmp_path,
            role=RuntimeAgentRole.IMPLEMENTER,
            namespace="",
            service=service,
            role_session=role_session,
            provider_session_adapter=_ServiceBackedRuntimeProviderSessionAdapter(
                service
            ),
        )
    )

    assert plan.provider_state_dir_container_path(
        worktree=tmp_path,
        container_workspace="/workspace",
    ) == ("/workspace/.pycastle-session/implementer/generic/")


def test_runtime_provider_state_plan_falls_back_to_relpath_when_selected_state_dir_is_outside_worktree(
    tmp_path: Path,
) -> None:
    from pycastle.agents.output_protocol import AgentRole as RuntimeAgentRole
    from pycastle.runtime_session import ProviderSessionState, RunKind
    from pycastle.session_planning import (
        ProviderRunStatePlanRequest,
        plan_provider_run_state,
    )

    selected_state_dir = tmp_path.parent / "external-opencode-state"
    service_state_dir = tmp_path / ".pycastle-session" / "implementer" / "opencode"
    role_session = _RuntimeRoleSessionStandIn(
        _RuntimeServiceSessionState(
            state_dir=service_state_dir,
            has_resumable_provider_state=True,
            state_dir_relpath=".pycastle-session/implementer/opencode/",
        )
    )
    service = _PlanRecordingRuntimeService(
        "opencode",
        ProviderSessionState(
            RunKind.RESUME,
            "sess-opencode",
            state_dir_relpath="custom/opencode-state/",
            state_dir_path=selected_state_dir,
        ),
    )

    plan = plan_provider_run_state(
        ProviderRunStatePlanRequest(
            worktree=tmp_path,
            role=RuntimeAgentRole.IMPLEMENTER,
            namespace="",
            service=service,
            role_session=role_session,
            provider_session_adapter=_ServiceBackedRuntimeProviderSessionAdapter(
                service
            ),
        )
    )

    assert (
        plan.provider_state_dir_container_path(
            worktree=tmp_path,
            container_workspace="/workspace",
        )
        == "/workspace/custom/opencode-state/"
    )


def test_runtime_package_returns_assistant_turns_when_service_emits_no_result(
    tmp_path: Path,
):
    runtime = _runtime_namespace()

    managed_mount = _managed_mount(tmp_path)
    service = _RecordingRuntimeService(
        "codex",
        events=(
            AssistantTurn(text="first turn"),
            AssistantTurn(text="second turn"),
        ),
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client([]),
        service_registry={"codex": service},
    )
    registry = runtime.ServiceRegistry({"codex": service})
    request = runtime.PromptRunRequest(
        worktree=runtime.WorktreeMount(managed_mount),
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
    assert [getattr(policy, "value", None) for policy in service.tool_policies] == [
        runtime.ToolPolicy.FULL.value
    ]


def test_runtime_package_prompt_entrypoint_uses_injected_execution_adapter_contract(
    tmp_path: Path,
):
    runtime = _runtime_namespace()

    service = _RecordingRuntimeService("codex")
    adapter = _PromptRuntimeExecutionAdapterStandIn(
        git_service=_make_git_service(),
        service=service,
        session=_RuntimeSessionStandIn(),
    )
    registry = runtime.ServiceRegistry({"codex": service})
    request = runtime.PromptRunRequest(
        name="Runtime Consumer",
        worktree=runtime.WorktreeMount(tmp_path),
        prompt="Return the final answer only.",
        override=runtime.StageOverride(
            service="codex",
            model="gpt-5.4",
            effort="medium",
        ),
        tool_policy=runtime.ToolPolicy.PARTIAL,
    )

    result = asyncio.run(
        runtime.run_prompt(runner=adapter, service_registry=registry, request=request)
    )

    assert result == "adapter result"
    assert adapter.resolve_service_calls == ["codex"]
    assert adapter.build_work_dependency_calls == [
        ("Runtime Consumer", "gpt-5.4", "medium", service)
    ]
    assert adapter.work_runner.work_text_calls == [
        (
            AgentRole.IMPLEMENTER,
            runtime.ToolPolicy.PARTIAL,
            RunKind.FRESH,
            None,
            "Return the final answer only.",
        )
    ]
    assert not hasattr(adapter, "_resolve_service")
    assert not hasattr(adapter, "_build_session")


def test_runtime_package_prompt_entrypoint_preserves_runtime_owned_session_contract(
    tmp_path: Path,
):
    runtime = _runtime_namespace()

    service = _RecordingRuntimeService("codex")
    adapter = _PromptRuntimeExecutionAdapterStandIn(
        git_service=_make_git_service(),
        service=service,
        session=_RuntimeSessionStandIn(),
    )
    registry = runtime.ServiceRegistry({"codex": service})
    run_session_plan = {"resume": "provider-123"}
    request = runtime.PromptRunRequest(
        name="Runtime Consumer",
        worktree=runtime.WorktreeMount(tmp_path),
        prompt="Return the final answer only.",
        override=runtime.StageOverride(
            service="codex",
            model="gpt-5.4",
            effort="medium",
        ),
        session=runtime.PromptRunSession(
            namespace="issues",
            plan=run_session_plan,
        ),
    )

    result = asyncio.run(
        runtime.run_prompt(runner=adapter, service_registry=registry, request=request)
    )

    assert result == "adapter result"
    assert adapter.prepare_session_calls == [
        RunSessionPlan(
            mount_path=tmp_path,
            role=AgentRole.IMPLEMENTER,
            session_namespace="issues",
            service=service,
            container_workspace="/home/agent/workspace",
            run_session_plan=run_session_plan,
        )
    ]


def test_runtime_package_resident_entrypoint_executes_planned_resume_and_returns_session_aware_result(
    tmp_path: Path,
) -> None:
    runtime = _runtime_namespace()

    selected_state_dir = tmp_path / "custom" / "generic-state"
    role_session = _RuntimeRoleSessionStandIn(
        _RuntimeServiceSessionState(
            state_dir=tmp_path
            / ".pycastle-session"
            / "implementer"
            / "issues"
            / "codex",
            has_resumable_provider_state=True,
            state_dir_relpath=".pycastle-session/implementer/issues/codex/",
        )
    )
    provider_state = runtime.ProviderSessionState(
        runtime.RunKind.RESUME,
        "persisted-session-id",
        state_dir_relpath="custom/generic-state/",
        state_dir_path=selected_state_dir,
        exact_transcript_match=True,
        persist_provider_session_id=True,
    )
    service = _PlanRecordingRuntimeService("codex", provider_state)
    provider_session_adapter = _RecordingProviderSessionAdapter(
        provider_state,
        service_name="codex",
    )

    class _ResidentRunner(_RuntimeWorkRunnerStandIn):
        async def work_text(
            self,
            prompt: str,
            *,
            role: AgentRole = AgentRole.IMPLEMENTER,
            tool_policy: object = "full",
            run_kind: RunKind = RunKind.FRESH,
            session_uuid: str | None = None,
            on_provider_session_id: Callable[[str], None] | None = None,
        ) -> str:
            if on_provider_session_id is not None:
                on_provider_session_id("runtime-session-id")
            self.work_text_calls.append(
                (role, tool_policy, run_kind, session_uuid, prompt)
            )
            return self._result

    adapter = _PromptRuntimeExecutionAdapterStandIn(
        git_service=_make_git_service(),
        service=service,
        session=_RuntimeSessionStandIn(),
        runner=_ResidentRunner(),
    )
    plan = runtime.plan_resident_session(
        runtime.ResidentSessionPlanRequest(
            worktree=tmp_path,
            role=runtime.AgentRole.IMPLEMENTER,
            namespace="issues",
            service=service,
            role_session=role_session,
            provider_session_adapter=provider_session_adapter,
        )
    )

    result = asyncio.run(
        runtime.run_resident_prompt(
            runner=adapter,
            request=runtime.ResidentRunRequest(
                name="Runtime Consumer",
                prompt="Continue from prior context.",
                worktree=runtime.WorktreeMount(tmp_path),
                model="gpt-5.4",
                effort="medium",
                tool_policy=runtime.ToolPolicy.PARTIAL,
                session_plan=plan,
            ),
        )
    )

    assert result == runtime.ResidentRunResult(
        output="adapter result",
        runtime_metadata=runtime.ResidentRuntimeMetadata(
            service_name="codex",
            provider_session_id="runtime-session-id",
            run_kind=runtime.RunKind.RESUME,
            session_namespace="issues",
            exact_transcript_match=True,
        ),
    )
    assert provider_session_adapter.prepare_calls == [(selected_state_dir, None)]
    assert role_session.saved_service_session_ids == [
        ("codex", "persisted-session-id"),
        ("codex", "runtime-session-id"),
    ]
    assert role_session.recorded_success_metadata == [("codex", "runtime-session-id")]
    assert adapter.prepare_session_calls == []
    assert adapter.work_runner.work_text_calls == [
        (
            AgentRole.IMPLEMENTER,
            runtime.ToolPolicy.PARTIAL,
            RunKind.RESUME,
            "persisted-session-id",
            "Continue from prior context.",
        )
    ]


def test_runtime_package_resident_entrypoint_executes_planned_fresh_run_and_returns_fresh_session_metadata(
    tmp_path: Path,
) -> None:
    runtime = _runtime_namespace()

    role_session = _RuntimeRoleSessionStandIn(
        _RuntimeServiceSessionState(
            state_dir=tmp_path / ".pycastle-session" / "implementer" / "main" / "codex",
            has_resumable_provider_state=False,
            state_dir_relpath=".pycastle-session/implementer/main/codex/",
        )
    )
    provider_state = runtime.ProviderSessionState(runtime.RunKind.FRESH, None)
    service = _PlanRecordingRuntimeService("codex", provider_state)
    provider_session_adapter = _RecordingProviderSessionAdapter(
        provider_state,
        service_name="codex",
    )

    class _ResidentRunner(_RuntimeWorkRunnerStandIn):
        async def work_text(
            self,
            prompt: str,
            *,
            role: AgentRole = AgentRole.IMPLEMENTER,
            tool_policy: object = "full",
            run_kind: RunKind = RunKind.FRESH,
            session_uuid: str | None = None,
            on_provider_session_id: Callable[[str], None] | None = None,
        ) -> str:
            if on_provider_session_id is not None:
                on_provider_session_id("fresh-runtime-session-id")
            self.work_text_calls.append(
                (role, tool_policy, run_kind, session_uuid, prompt)
            )
            return self._result

    adapter = _PromptRuntimeExecutionAdapterStandIn(
        git_service=_make_git_service(),
        service=service,
        session=_RuntimeSessionStandIn(),
        runner=_ResidentRunner(),
    )
    plan = runtime.plan_resident_session(
        runtime.ResidentSessionPlanRequest(
            worktree=tmp_path,
            role=runtime.AgentRole.IMPLEMENTER,
            namespace="main",
            service=service,
            role_session=role_session,
            provider_session_adapter=provider_session_adapter,
        )
    )

    result = asyncio.run(
        runtime.run_resident_prompt(
            runner=adapter,
            request=runtime.ResidentRunRequest(
                name="Runtime Consumer",
                prompt="Start a new session.",
                worktree=runtime.WorktreeMount(tmp_path),
                model="gpt-5.4",
                effort="medium",
                session_plan=plan,
            ),
        )
    )

    assert result == runtime.ResidentRunResult(
        output="adapter result",
        runtime_metadata=runtime.ResidentRuntimeMetadata(
            service_name="codex",
            provider_session_id="fresh-runtime-session-id",
            run_kind=runtime.RunKind.FRESH,
            session_namespace="main",
            exact_transcript_match=False,
        ),
    )
    assert provider_session_adapter.prepare_calls == [
        (plan.host_provider_state_dir, None)
    ]
    assert role_session.saved_service_session_ids == [
        ("codex", "fresh-runtime-session-id")
    ]
    assert role_session.recorded_success_metadata == [
        ("codex", "fresh-runtime-session-id")
    ]
    assert adapter.prepare_session_calls == []
    assert adapter.work_runner.work_text_calls == [
        (
            AgentRole.IMPLEMENTER,
            runtime.ToolPolicy.FULL,
            RunKind.FRESH,
            None,
            "Start a new session.",
        )
    ]


def test_runtime_package_resident_entrypoint_returns_prepared_provider_session_id_when_execution_keeps_existing_session(
    tmp_path: Path,
) -> None:
    runtime = _runtime_namespace()

    selected_state_dir = tmp_path / "custom" / "generic-state"
    role_session = _RuntimeRoleSessionStandIn(
        _RuntimeServiceSessionState(
            state_dir=tmp_path
            / ".pycastle-session"
            / "implementer"
            / "issues"
            / "codex",
            has_resumable_provider_state=True,
            state_dir_relpath=".pycastle-session/implementer/issues/codex/",
        )
    )
    provider_state = runtime.ProviderSessionState(
        runtime.RunKind.RESUME,
        "persisted-session-id",
        state_dir_relpath="custom/generic-state/",
        state_dir_path=selected_state_dir,
        exact_transcript_match=True,
        persist_provider_session_id=True,
    )
    service = _PlanRecordingRuntimeService("codex", provider_state)
    provider_session_adapter = _RecordingProviderSessionAdapter(
        provider_state,
        service_name="codex",
    )
    adapter = _PromptRuntimeExecutionAdapterStandIn(
        git_service=_make_git_service(),
        service=service,
        session=_RuntimeSessionStandIn(),
    )
    plan = runtime.plan_resident_session(
        runtime.ResidentSessionPlanRequest(
            worktree=tmp_path,
            role=runtime.AgentRole.IMPLEMENTER,
            namespace="issues",
            service=service,
            role_session=role_session,
            provider_session_adapter=provider_session_adapter,
        )
    )

    result = asyncio.run(
        runtime.run_resident_prompt(
            runner=adapter,
            request=runtime.ResidentRunRequest(
                name="Runtime Consumer",
                prompt="Continue from prior context.",
                worktree=runtime.WorktreeMount(tmp_path),
                model="gpt-5.4",
                effort="medium",
                session_plan=plan,
            ),
        )
    )

    assert result == runtime.ResidentRunResult(
        output="adapter result",
        runtime_metadata=runtime.ResidentRuntimeMetadata(
            service_name="codex",
            provider_session_id="persisted-session-id",
            run_kind=runtime.RunKind.RESUME,
            session_namespace="issues",
            exact_transcript_match=True,
        ),
    )
    assert provider_session_adapter.prepare_calls == [(selected_state_dir, None)]
    assert role_session.saved_service_session_ids == [("codex", "persisted-session-id")]
    assert role_session.recorded_success_metadata == [("codex", "persisted-session-id")]
    assert adapter.prepare_session_calls == []
    assert adapter.work_runner.work_text_calls == [
        (
            AgentRole.IMPLEMENTER,
            runtime.ToolPolicy.FULL,
            RunKind.RESUME,
            "persisted-session-id",
            "Continue from prior context.",
        )
    ]


def test_runtime_package_one_shot_entrypoint_resolves_stage_chain_and_returns_selected_runtime_result(
    tmp_path: Path,
) -> None:
    runtime = _runtime_namespace()

    service = _RecordingRuntimeService("codex")
    adapter = _PromptRuntimeExecutionAdapterStandIn(
        git_service=_make_git_service(),
        service=service,
        session=_RuntimeSessionStandIn(),
    )

    class _RawOutputRunner:
        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind = RunKind.FRESH,
            session_uuid: str | None = None,
            on_provider_session_id: Callable[[str], None] | None = None,
        ) -> dict[str, object]:
            del session_uuid
            if on_provider_session_id is not None:
                on_provider_session_id("provider-session")
            return {
                "role": role.value,
                "prompt": prompt,
                "run_kind": run_kind.value,
                "events": [{"type": "assistant", "text": "raw turn"}],
            }

        async def setup(
            self, git_name: str, git_email: str, work_body: str = ""
        ) -> None:
            del git_name, git_email, work_body

        async def work_text(
            self,
            prompt: str,
            *,
            role: AgentRole = AgentRole.IMPLEMENTER,
            tool_policy: object = "full",
            run_kind: RunKind = RunKind.FRESH,
            session_uuid: str | None = None,
            on_provider_session_id: Callable[[str], None] | None = None,
        ) -> str:
            del (
                prompt,
                role,
                tool_policy,
                run_kind,
                session_uuid,
                on_provider_session_id,
            )
            raise AssertionError("one-shot runtime should preserve raw output")

    adapter.work_runner = cast(Any, _RawOutputRunner())
    registry = runtime.ServiceRegistry({"codex": service})
    request = runtime.OneShotRunRequest(
        name="Runtime Consumer",
        worktree=runtime.WorktreeMount(tmp_path),
        prompt="Return JSON only.",
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
        runtime.run_one_shot(
            runner=adapter,
            service_registry=registry,
            request=request,
        )
    )

    assert result == runtime.OneShotRunResult(
        selected_service="codex",
        selected_model="gpt-5.4",
        selected_effort="medium",
        used_fallback=True,
        selected_service_path=("missing", "codex"),
        raw_output={
            "role": AgentRole.IMPLEMENTER.value,
            "prompt": "Return JSON only.",
            "run_kind": RunKind.FRESH.value,
            "events": [{"type": "assistant", "text": "raw turn"}],
        },
        runtime_metadata=runtime.OneShotRuntimeMetadata(
            provider_session_id="provider-session",
            run_kind=runtime.RunKind.FRESH,
            session_namespace="",
        ),
    )


def test_runtime_package_one_shot_entrypoint_preserves_resume_runtime_metadata(
    tmp_path: Path,
) -> None:
    runtime = _runtime_namespace()

    codex_service = _RecordingRuntimeService("codex")

    class _ResumeAwareAdapter:
        def resolve_service(self, service_name: str = "") -> _RecordingRuntimeService:
            assert service_name == "codex"
            return codex_service

        def build_work_dependencies(
            self,
            *,
            name: str,
            model: str,
            effort: str,
            service: _RecordingRuntimeService,
        ) -> WorkInvocationDependencies:
            assert name == "Runtime Consumer"
            assert model == "gpt-5.4"
            assert effort == "medium"
            assert service is codex_service

            class _ResumeRunner:
                async def setup(
                    self,
                    git_name: str,
                    git_email: str,
                    work_body: str = "",
                ) -> None:
                    del git_name, git_email, work_body

                async def work(
                    self,
                    role: AgentRole,
                    prompt: str,
                    *,
                    run_kind: RunKind = RunKind.FRESH,
                    session_uuid: str | None = None,
                    on_provider_session_id: Callable[[str], None] | None = None,
                ) -> dict[str, object]:
                    del role, on_provider_session_id
                    return {
                        "prompt": prompt,
                        "run_kind": run_kind.value,
                        "session_uuid": session_uuid,
                    }

                async def work_text(
                    self,
                    prompt: str,
                    *,
                    role: AgentRole = AgentRole.IMPLEMENTER,
                    tool_policy: object = "full",
                    run_kind: RunKind = RunKind.FRESH,
                    session_uuid: str | None = None,
                    on_provider_session_id: Callable[[str], None] | None = None,
                ) -> str:
                    del (
                        prompt,
                        role,
                        tool_policy,
                        run_kind,
                        session_uuid,
                        on_provider_session_id,
                    )
                    raise AssertionError("one-shot runtime should preserve raw output")

            def _prepare_session(
                run_session_plan: RunSessionPlan,
            ) -> _PreparedRuntimeSessionStandIn:
                del run_session_plan
                prepared = _PreparedRuntimeSessionStandIn()
                prepared.initial_session.run_kind = RunKind.RESUME
                prepared.initial_session.provider_session_id = "provider-resume"
                return prepared

            return WorkInvocationDependencies(
                container_workspace="/home/agent/workspace",
                timeout_retries=0,
                stage_key_for_role=lambda role: role.value,
                prepare_session=_prepare_session,
                build_session=lambda *_args: _RuntimeSessionStandIn(),
                build_runner=lambda *_args: cast(Any, _ResumeRunner()),
                get_git_identity=lambda: ("Alice", "alice@example.com"),
            )

    registry = runtime.ServiceRegistry({"codex": codex_service})
    request = runtime.OneShotRunRequest(
        name="Runtime Consumer",
        worktree=runtime.WorktreeMount(tmp_path),
        prompt="Return JSON only.",
        override=runtime.StageOverride(
            service="codex",
            model="gpt-5.4",
            effort="medium",
        ),
        session=runtime.PromptRunSession(
            namespace="issues",
            plan={"resume": "provider-resume"},
        ),
    )

    result = asyncio.run(
        runtime.run_one_shot(
            runner=cast(Any, _ResumeAwareAdapter()),
            service_registry=registry,
            request=request,
        )
    )

    assert result == runtime.OneShotRunResult(
        selected_service="codex",
        selected_model="gpt-5.4",
        selected_effort="medium",
        used_fallback=False,
        selected_service_path=("codex",),
        raw_output={
            "prompt": "Return JSON only.",
            "run_kind": runtime.RunKind.RESUME.value,
            "session_uuid": "provider-resume",
        },
        runtime_metadata=runtime.OneShotRuntimeMetadata(
            provider_session_id="provider-resume",
            run_kind=runtime.RunKind.RESUME,
            session_namespace="issues",
        ),
    )


def test_runtime_package_one_shot_entrypoint_reports_no_fallback_for_unset_primary_service(
    tmp_path: Path,
) -> None:
    runtime = _runtime_namespace()

    codex_service = _RecordingRuntimeService("codex")

    class _UnsetPrimaryAdapter:
        def resolve_service(self, service_name: str = "") -> _RecordingRuntimeService:
            assert service_name == "codex"
            return codex_service

        def build_work_dependencies(
            self,
            *,
            name: str,
            model: str,
            effort: str,
            service: _RecordingRuntimeService,
        ) -> WorkInvocationDependencies:
            assert name == "Runtime Consumer"
            assert model == "gpt-5.4"
            assert effort == "medium"
            assert service is codex_service

            class _Runner:
                async def setup(
                    self,
                    git_name: str,
                    git_email: str,
                    work_body: str = "",
                ) -> None:
                    del git_name, git_email, work_body

                async def work(
                    self,
                    role: AgentRole,
                    prompt: str,
                    *,
                    run_kind: RunKind = RunKind.FRESH,
                    session_uuid: str | None = None,
                    on_provider_session_id: Callable[[str], None] | None = None,
                ) -> dict[str, object]:
                    del (
                        role,
                        prompt,
                        run_kind,
                        session_uuid,
                        on_provider_session_id,
                    )
                    return {"service": service.name}

                async def work_text(
                    self,
                    prompt: str,
                    *,
                    role: AgentRole = AgentRole.IMPLEMENTER,
                    tool_policy: object = "full",
                    run_kind: RunKind = RunKind.FRESH,
                    session_uuid: str | None = None,
                    on_provider_session_id: Callable[[str], None] | None = None,
                ) -> str:
                    del (
                        prompt,
                        role,
                        tool_policy,
                        run_kind,
                        session_uuid,
                        on_provider_session_id,
                    )
                    raise AssertionError("one-shot runtime should preserve raw output")

            return WorkInvocationDependencies(
                container_workspace="/home/agent/workspace",
                timeout_retries=0,
                stage_key_for_role=lambda role: role.value,
                prepare_session=lambda _plan: _PreparedRuntimeSessionStandIn(),
                build_session=lambda *_args: _RuntimeSessionStandIn(),
                build_runner=lambda *_args: cast(Any, _Runner()),
                get_git_identity=lambda: ("Alice", "alice@example.com"),
            )

    request = runtime.OneShotRunRequest(
        name="Runtime Consumer",
        worktree=runtime.WorktreeMount(tmp_path),
        prompt="Return JSON only.",
        override=runtime.StageOverride(
            fallback=runtime.StageOverride(
                service="codex",
                model="gpt-5.4",
                effort="medium",
            )
        ),
    )

    result = asyncio.run(
        runtime.run_one_shot(
            runner=cast(Any, _UnsetPrimaryAdapter()),
            service_registry=runtime.ServiceRegistry({"codex": codex_service}),
            request=request,
        )
    )

    assert result == runtime.OneShotRunResult(
        selected_service="codex",
        selected_model="gpt-5.4",
        selected_effort="medium",
        used_fallback=False,
        selected_service_path=("codex",),
        raw_output={"service": "codex"},
        runtime_metadata=runtime.OneShotRuntimeMetadata(
            provider_session_id=None,
            run_kind=runtime.RunKind.FRESH,
            session_namespace="",
        ),
    )


def test_runtime_package_one_shot_entrypoint_falls_through_on_usage_limit_with_shared_cancellation_token(
    tmp_path: Path,
) -> None:
    runtime = _runtime_namespace()

    class _ExhaustibleRuntimeService(_RecordingRuntimeService):
        def __init__(self, name: str) -> None:
            super().__init__(name)
            self._available = True

        def is_available(self, now: datetime | None = None) -> bool:
            del now
            return self._available

        def mark_exhausted(self, reset_time: datetime | None) -> None:
            del reset_time
            self._available = False

    primary = _ExhaustibleRuntimeService("codex")
    fallback = _ExhaustibleRuntimeService("claude")
    observed_service_names: list[str] = []

    class _FallbackAdapter:
        def resolve_service(self, service_name: str = "") -> _RecordingRuntimeService:
            if service_name == "codex":
                return primary
            if service_name == "claude":
                return fallback
            raise AssertionError(f"unexpected service {service_name!r}")

        def build_work_dependencies(
            self,
            *,
            name: str,
            model: str,
            effort: str,
            service: _RecordingRuntimeService,
        ) -> WorkInvocationDependencies:
            del name, model, effort
            observed_service_names.append(service.name)

            class _UsageLimitThenFallbackRunner:
                async def setup(
                    self,
                    git_name: str,
                    git_email: str,
                    work_body: str = "",
                ) -> None:
                    del git_name, git_email, work_body

                async def work(
                    self,
                    role: AgentRole,
                    prompt: str,
                    *,
                    run_kind: RunKind = RunKind.FRESH,
                    session_uuid: str | None = None,
                    on_provider_session_id: Callable[[str], None] | None = None,
                ) -> dict[str, object]:
                    del role, prompt, run_kind, session_uuid
                    if service.name == "codex":
                        raise UsageLimitError(reset_time=datetime(2026, 1, 1))
                    if on_provider_session_id is not None:
                        on_provider_session_id("fallback-session")
                    return {"service": service.name, "result": "raw fallback result"}

                async def work_text(
                    self,
                    prompt: str,
                    *,
                    role: AgentRole = AgentRole.IMPLEMENTER,
                    tool_policy: object = "full",
                    run_kind: RunKind = RunKind.FRESH,
                    session_uuid: str | None = None,
                    on_provider_session_id: Callable[[str], None] | None = None,
                ) -> str:
                    del (
                        prompt,
                        role,
                        tool_policy,
                        run_kind,
                        session_uuid,
                        on_provider_session_id,
                    )
                    raise AssertionError("one-shot runtime should preserve raw output")

            return WorkInvocationDependencies(
                container_workspace="/home/agent/workspace",
                timeout_retries=0,
                stage_key_for_role=lambda role: role.value,
                prepare_session=lambda _plan: _PreparedRuntimeSessionStandIn(),
                build_session=lambda *_args: _RuntimeSessionStandIn(),
                build_runner=lambda *_args: cast(
                    Any,
                    _UsageLimitThenFallbackRunner(),
                ),
                get_git_identity=lambda: ("Alice", "alice@example.com"),
            )

    request = runtime.OneShotRunRequest(
        name="Runtime Consumer",
        worktree=runtime.WorktreeMount(tmp_path),
        prompt="Return JSON only.",
        override=runtime.StageOverride(
            service="codex",
            model="gpt-5.4",
            effort="medium",
            fallback=runtime.StageOverride(
                service="claude",
                model="sonnet",
                effort="high",
            ),
        ),
        token=runtime.CancellationToken(),
    )

    result = asyncio.run(
        runtime.run_one_shot(
            runner=cast(Any, _FallbackAdapter()),
            service_registry=runtime.ServiceRegistry(
                {"codex": primary, "claude": fallback}
            ),
            request=request,
        )
    )

    assert observed_service_names == ["codex", "claude"]
    assert result == runtime.OneShotRunResult(
        selected_service="claude",
        selected_model="sonnet",
        selected_effort="high",
        used_fallback=True,
        selected_service_path=("codex", "claude"),
        raw_output={"service": "claude", "result": "raw fallback result"},
        runtime_metadata=runtime.OneShotRuntimeMetadata(
            provider_session_id="fallback-session",
            run_kind=runtime.RunKind.FRESH,
            session_namespace="",
        ),
    )


def test_runtime_package_invoke_work_dispatches_through_canonical_run_session_plan(
    tmp_path: Path,
) -> None:
    service = _RecordingRuntimeService("codex")
    mirrored_service = _RecordingRuntimeService("claude")
    runner = _RuntimeWorkRunnerStandIn()
    observed_session_builds: list[tuple[Path, object, str | None]] = []
    observed_prepare_plans: list[RunSessionPlan] = []

    class _PreparedResumeSession(_PreparedRuntimeSessionStandIn):
        def __init__(self) -> None:
            super().__init__()
            self.initial_session.run_kind = RunKind.RESUME
            self.initial_session.provider_session_id = "provider-resume"

    def _prepare_session(run_session: RunSessionPlan) -> _PreparedResumeSession:
        observed_prepare_plans.append(run_session)
        return _PreparedResumeSession()

    def _build_session(
        mount_path: Path,
        selected_service: object,
        state_dir: str | None,
    ) -> _RuntimeSessionStandIn:
        observed_session_builds.append((mount_path, selected_service, state_dir))
        return _RuntimeSessionStandIn()

    canonical_run_session = RunSessionPlan(
        mount_path=tmp_path / "canonical",
        role=AgentRole.IMPLEMENTER,
        session_namespace="issues",
        service=service,
        container_workspace="/workspace/canonical",
        run_session_plan={"resume": "provider-resume"},
    )

    result = asyncio.run(
        invoke_work(
            WorkInvocationRequest(
                name="Runtime Consumer",
                mount_path=tmp_path / "mirrored",
                role=AgentRole.REVIEWER,
                service=mirrored_service,
                model="gpt-5.4",
                effort="medium",
                output_adapter=TextOutputAdapter(prompt="runtime prompt"),
                dependencies=WorkInvocationDependencies(
                    container_workspace="/workspace/mirrored",
                    timeout_retries=0,
                    stage_key_for_role=lambda role: role.value,
                    prepare_session=_prepare_session,
                    build_session=_build_session,
                    build_runner=lambda *_args: runner,
                    get_git_identity=lambda: ("Alice", "alice@example.com"),
                ),
                session_namespace="mirrored",
                run_session_plan={"resume": "mirrored"},
                run_session=canonical_run_session,
            )
        )
    )

    assert result == "adapter result"
    assert observed_prepare_plans == [canonical_run_session]
    assert observed_session_builds == [
        (canonical_run_session.mount_path, service, None)
    ]
    assert len(runner.work_text_calls) == 1
    role, tool_policy, run_kind, session_uuid, prompt = runner.work_text_calls[0]
    assert role is AgentRole.IMPLEMENTER
    assert getattr(tool_policy, "value", tool_policy) == "full"
    assert run_kind is RunKind.RESUME
    assert session_uuid == "provider-resume"
    assert prompt == "runtime prompt"


def test_runtime_package_prompt_entrypoint_fills_missing_credential_failure_service_name_from_selected_fallback(
    tmp_path: Path,
) -> None:
    runtime = _runtime_namespace()

    service = _RecordingRuntimeService("opencode")
    adapter = _PromptRuntimeExecutionAdapterStandIn(
        git_service=_make_git_service(),
        service=service,
        session=_RuntimeSessionStandIn(),
    )

    class _CredentialFailureRunner(_RuntimeWorkRunnerStandIn):
        async def work_text(
            self,
            prompt: str,
            *,
            role: AgentRole = AgentRole.IMPLEMENTER,
            tool_policy: object = "full",
            run_kind: RunKind = RunKind.FRESH,
            session_uuid: str | None = None,
            on_provider_session_id: Callable[[str], None] | None = None,
        ) -> str:
            del (
                prompt,
                role,
                tool_policy,
                run_kind,
                session_uuid,
                on_provider_session_id,
            )
            raise AgentCredentialFailureError(
                "credential failure from provider adapter",
                status_code=401,
                service_name="",
            )

    adapter.work_runner = _CredentialFailureRunner()
    registry = runtime.ServiceRegistry({"opencode": service})
    request = runtime.PromptRunRequest(
        name="Runtime Consumer",
        worktree=runtime.WorktreeMount(tmp_path),
        prompt="Return the final answer only.",
        override=runtime.StageOverride(
            service="missing",
            model="ignored",
            effort="medium",
            fallback=runtime.StageOverride(
                service="opencode",
                model="gpt-5.4",
                effort="medium",
            ),
        ),
    )

    with pytest.raises(AgentCredentialFailureError) as excinfo:
        asyncio.run(
            runtime.run_prompt(
                runner=adapter,
                service_registry=registry,
                request=request,
            )
        )

    assert excinfo.value.service_name == "opencode"


def test_runtime_package_prompt_entrypoint_preserves_provider_named_credential_failure_from_selected_fallback(
    tmp_path: Path,
) -> None:
    runtime = _runtime_namespace()

    service = _RecordingRuntimeService("opencode")
    adapter = _PromptRuntimeExecutionAdapterStandIn(
        git_service=_make_git_service(),
        service=service,
        session=_RuntimeSessionStandIn(),
    )

    class _CredentialFailureRunner(_RuntimeWorkRunnerStandIn):
        async def work_text(
            self,
            prompt: str,
            *,
            role: AgentRole = AgentRole.IMPLEMENTER,
            tool_policy: object = "full",
            run_kind: RunKind = RunKind.FRESH,
            session_uuid: str | None = None,
            on_provider_session_id: Callable[[str], None] | None = None,
        ) -> str:
            del (
                prompt,
                role,
                tool_policy,
                run_kind,
                session_uuid,
                on_provider_session_id,
            )
            raise AgentCredentialFailureError(
                "credential failure from provider adapter",
                status_code=401,
                service_name="claude",
            )

    adapter.work_runner = _CredentialFailureRunner()
    registry = runtime.ServiceRegistry({"opencode": service})
    request = runtime.PromptRunRequest(
        name="Runtime Consumer",
        worktree=runtime.WorktreeMount(tmp_path),
        prompt="Return the final answer only.",
        override=runtime.StageOverride(
            service="missing",
            model="ignored",
            effort="medium",
            fallback=runtime.StageOverride(
                service="opencode",
                model="gpt-5.4",
                effort="medium",
            ),
        ),
    )

    with pytest.raises(AgentCredentialFailureError) as excinfo:
        asyncio.run(
            runtime.run_prompt(
                runner=adapter,
                service_registry=registry,
                request=request,
            )
        )

    assert excinfo.value.service_name == "claude"


def test_runtime_package_default_status_row_preserves_usage_limit_shutdown_message(
    tmp_path: Path,
) -> None:
    status_display = _RecordingStatusDisplay()

    class _UsageLimitRunner(_RuntimeWorkRunnerStandIn):
        async def work_text(
            self,
            prompt: str,
            *,
            role: AgentRole = AgentRole.IMPLEMENTER,
            tool_policy: object = "full",
            run_kind: RunKind = RunKind.FRESH,
            session_uuid: str | None = None,
            on_provider_session_id: Callable[[str], None] | None = None,
        ) -> str:
            del (
                prompt,
                role,
                tool_policy,
                run_kind,
                session_uuid,
                on_provider_session_id,
            )
            raise UsageLimitError(reset_time=None)

    with pytest.raises(UsageLimitError):
        asyncio.run(
            invoke_work(
                WorkInvocationRequest(
                    name="Runtime Consumer",
                    mount_path=tmp_path,
                    role=AgentRole.IMPLEMENTER,
                    service=_RecordingRuntimeService("codex"),
                    model="gpt-5.4",
                    effort="medium",
                    output_adapter=TextOutputAdapter(prompt="runtime prompt"),
                    dependencies=WorkInvocationDependencies(
                        container_workspace="/home/agent/workspace",
                        timeout_retries=0,
                        stage_key_for_role=lambda role: role.value,
                        prepare_session=lambda _plan: _PreparedRuntimeSessionStandIn(),
                        build_session=lambda *_args: _RuntimeSessionStandIn(),
                        build_runner=lambda *_args: _UsageLimitRunner(),
                        get_git_identity=lambda: ("Alice", "alice@example.com"),
                        status_display_factory=lambda: status_display,
                    ),
                )
            )
        )

    assert status_display.remove_calls == [
        ("Runtime Consumer", "usage limit reached", "interrupted")
    ]


def test_runtime_package_default_status_row_preserves_timeout_shutdown_message(
    tmp_path: Path,
) -> None:
    status_display = _RecordingStatusDisplay()

    class _TimeoutRunner(_RuntimeWorkRunnerStandIn):
        async def work_text(
            self,
            prompt: str,
            *,
            role: AgentRole = AgentRole.IMPLEMENTER,
            tool_policy: object = "full",
            run_kind: RunKind = RunKind.FRESH,
            session_uuid: str | None = None,
            on_provider_session_id: Callable[[str], None] | None = None,
        ) -> str:
            del (
                prompt,
                role,
                tool_policy,
                run_kind,
                session_uuid,
                on_provider_session_id,
            )
            raise AgentTimeoutError("timed out")

    with pytest.raises(AgentTimeoutError):
        asyncio.run(
            invoke_work(
                WorkInvocationRequest(
                    name="Runtime Consumer",
                    mount_path=tmp_path,
                    role=AgentRole.IMPLEMENTER,
                    service=_RecordingRuntimeService("codex"),
                    model="gpt-5.4",
                    effort="medium",
                    output_adapter=TextOutputAdapter(prompt="runtime prompt"),
                    dependencies=WorkInvocationDependencies(
                        container_workspace="/home/agent/workspace",
                        timeout_retries=0,
                        stage_key_for_role=lambda role: role.value,
                        prepare_session=lambda _plan: _PreparedRuntimeSessionStandIn(),
                        build_session=lambda *_args: _RuntimeSessionStandIn(),
                        build_runner=lambda *_args: _TimeoutRunner(),
                        get_git_identity=lambda: ("Alice", "alice@example.com"),
                        status_display_factory=lambda: status_display,
                    ),
                )
            )
        )

    assert status_display.remove_calls == [
        ("Runtime Consumer", "timed out", "interrupted")
    ]


def test_runtime_package_invoke_work_preserves_provider_named_credential_failure(
    tmp_path: Path,
) -> None:
    class _CredentialFailureRunner(_RuntimeWorkRunnerStandIn):
        async def work_text(
            self,
            prompt: str,
            *,
            role: AgentRole = AgentRole.IMPLEMENTER,
            tool_policy: object = "full",
            run_kind: RunKind = RunKind.FRESH,
            session_uuid: str | None = None,
            on_provider_session_id: Callable[[str], None] | None = None,
        ) -> str:
            del (
                prompt,
                role,
                tool_policy,
                run_kind,
                session_uuid,
                on_provider_session_id,
            )
            raise AgentCredentialFailureError(
                "credential failure from provider adapter",
                status_code=401,
                service_name="claude",
            )

    with pytest.raises(AgentCredentialFailureError) as excinfo:
        asyncio.run(
            invoke_work(
                WorkInvocationRequest(
                    name="Runtime Consumer",
                    mount_path=tmp_path,
                    role=AgentRole.IMPLEMENTER,
                    service=_RecordingRuntimeService("opencode"),
                    model="gpt-5.4",
                    effort="medium",
                    output_adapter=TextOutputAdapter(prompt="runtime prompt"),
                    dependencies=WorkInvocationDependencies(
                        container_workspace="/home/agent/workspace",
                        timeout_retries=0,
                        stage_key_for_role=lambda role: role.value,
                        prepare_session=lambda _plan: _PreparedRuntimeSessionStandIn(),
                        build_session=lambda *_args: _RuntimeSessionStandIn(),
                        build_runner=lambda *_args: _CredentialFailureRunner(),
                        get_git_identity=lambda: ("Alice", "alice@example.com"),
                    ),
                )
            )
        )

    assert str(excinfo.value) == "credential failure from provider adapter"
    assert excinfo.value.status_code == 401
    assert excinfo.value.service_name == "claude"


def test_runtime_package_invoke_work_fills_missing_credential_failure_service_name(
    tmp_path: Path,
) -> None:
    class _CredentialFailureRunner(_RuntimeWorkRunnerStandIn):
        async def work_text(
            self,
            prompt: str,
            *,
            role: AgentRole = AgentRole.IMPLEMENTER,
            tool_policy: object = "full",
            run_kind: RunKind = RunKind.FRESH,
            session_uuid: str | None = None,
            on_provider_session_id: Callable[[str], None] | None = None,
        ) -> str:
            del (
                prompt,
                role,
                tool_policy,
                run_kind,
                session_uuid,
                on_provider_session_id,
            )
            raise AgentCredentialFailureError(
                "credential failure from provider adapter",
                status_code=401,
                service_name="",
            )

    with pytest.raises(AgentCredentialFailureError) as excinfo:
        asyncio.run(
            invoke_work(
                WorkInvocationRequest(
                    name="Runtime Consumer",
                    mount_path=tmp_path,
                    role=AgentRole.IMPLEMENTER,
                    service=_RecordingRuntimeService("opencode"),
                    model="gpt-5.4",
                    effort="medium",
                    output_adapter=TextOutputAdapter(prompt="runtime prompt"),
                    dependencies=WorkInvocationDependencies(
                        container_workspace="/home/agent/workspace",
                        timeout_retries=0,
                        stage_key_for_role=lambda role: role.value,
                        prepare_session=lambda _plan: _PreparedRuntimeSessionStandIn(),
                        build_session=lambda *_args: _RuntimeSessionStandIn(),
                        build_runner=lambda *_args: _CredentialFailureRunner(),
                        get_git_identity=lambda: ("Alice", "alice@example.com"),
                    ),
                )
            )
        )

    assert str(excinfo.value) == "credential failure from provider adapter"
    assert excinfo.value.status_code == 401
    assert excinfo.value.service_name == "opencode"


def test_runtime_package_owns_service_selection_contract() -> None:
    runtime = _runtime_namespace()

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

    assert runtime.ServiceRegistry.__module__.startswith("pycastle.services")
    assert resolved == runtime.StageOverride(
        service="claude",
        model="sonnet",
        effort="high",
    )


def test_runtime_package_service_registry_snapshots_availability_per_configured_service() -> (
    None
):
    runtime = _runtime_namespace()

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
    runtime = _runtime_namespace()

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
    runtime = _runtime_namespace()

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
    runtime = _runtime_namespace()

    managed_mount = tmp_path / "pycastle" / ".worktrees" / "issue-1"
    managed_mount.mkdir(parents=True)
    (
        tmp_path / ".pycastle-session" / "implementer" / "codex" / "ignored-session"
    ).mkdir(parents=True)

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

    execution_adapter = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client([b'{"result":"runtime result"}\n']),
        service_registry={
            "codex": requested_service,
            "claude": fallback_service,
        },
    )
    prompt_runtime = runtime.PromptRuntime(
        execution_adapter=execution_adapter,
        service_registry={
            "codex": requested_service,
            "claude": fallback_service,
        },
    )
    request = runtime.PromptRunRequest(
        name="Runtime Consumer",
        worktree=runtime.WorktreeMount(managed_mount),
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
    assert [
        getattr(policy, "value", None) for policy in requested_service.tool_policies
    ] == [runtime.ToolPolicy.PARTIAL.value]
    [state_dir_container_path] = requested_service.state_dir_container_paths
    assert state_dir_container_path is not None
    assert state_dir_container_path.rstrip("/") == (
        "/home/agent/workspace/.pycastle-session/implementer/codex"
    )
    assert (managed_mount / ".pycastle-session" / "implementer" / "codex").is_dir()

    [log_path] = list(tmp_path.glob("runtime-consumer-*.log"))
    log_text = log_path.read_text(encoding="utf-8")
    assert '"prompt": "Return the final answer only."' in log_text
    assert '"result":"runtime result"' in log_text


def test_agent_runner_run_prompt_rejects_non_managed_mount_before_provider_setup(
    tmp_path: Path,
) -> None:
    requested_service = _StateDirRecordingRuntimeService(
        "codex",
        relpath=".pycastle-session/implementer/codex/",
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client([b'{"result":"runtime result"}\n']),
        service_registry={"codex": requested_service},
    )

    with pytest.raises(Exception) as excinfo:
        asyncio.run(
            runner.run_prompt(
                name="Runtime Consumer",
                prompt="Return the final answer only.",
                mount_path=tmp_path,
                model="gpt-5.4",
                effort="medium",
                service="codex",
            )
        )

    assert type(excinfo.value).__name__ == "ManagedWorktreeMountPreconditionError"
    assert "Runtime Consumer" in str(excinfo.value)
    assert "role 'implementer'" in str(excinfo.value)
    assert "pycastle/.worktrees" in str(excinfo.value)
    assert requested_service.tool_policies == []
    assert requested_service.state_dir_container_paths == []


def test_agent_runner_run_prompt_rejects_non_directory_managed_mount_before_provider_setup(
    tmp_path: Path,
) -> None:
    mount_path = tmp_path / "pycastle" / ".worktrees" / "issue-1"
    mount_path.parent.mkdir(parents=True)
    mount_path.write_text("not a directory", encoding="utf-8")

    requested_service = _StateDirRecordingRuntimeService(
        "codex",
        relpath=".pycastle-session/implementer/codex/",
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client([b'{"result":"runtime result"}\n']),
        service_registry={"codex": requested_service},
    )

    with pytest.raises(Exception) as excinfo:
        asyncio.run(
            runner.run_prompt(
                name="Runtime Consumer",
                prompt="Return the final answer only.",
                mount_path=mount_path,
                model="gpt-5.4",
                effort="medium",
                service="codex",
            )
        )

    assert type(excinfo.value).__name__ == "ManagedWorktreeMountPreconditionError"
    assert "Runtime Consumer" in str(excinfo.value)
    assert "mount_path_not_directory" in str(excinfo.value)
    assert requested_service.tool_policies == []
    assert requested_service.state_dir_container_paths == []


def test_runtime_package_resident_entrypoint_rejects_non_managed_mount_before_provider_setup(
    tmp_path: Path,
) -> None:
    runtime = _runtime_namespace()

    provider_state_dir = (
        tmp_path / ".pycastle-session" / "implementer" / "main" / "codex"
    )
    role_session = _RuntimeRoleSessionStandIn(
        _RuntimeServiceSessionState(
            state_dir=provider_state_dir,
            has_resumable_provider_state=True,
            state_dir_relpath=".pycastle-session/implementer/main/codex/",
        )
    )
    provider_state = runtime.ProviderSessionState(
        runtime.RunKind.RESUME,
        "persisted-session-id",
        state_dir_relpath=".pycastle-session/implementer/main/codex/",
        state_dir_path=provider_state_dir,
        exact_transcript_match=True,
        persist_provider_session_id=True,
    )
    service = _PlanRecordingRuntimeService("codex", provider_state)
    provider_session_adapter = _RecordingProviderSessionAdapter(
        provider_state,
        service_name="codex",
    )
    execution_adapter = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client([b'{"result":"runtime result"}\n']),
        service_registry={"codex": service},
    )
    plan = runtime.plan_resident_session(
        runtime.ResidentSessionPlanRequest(
            worktree=tmp_path,
            role=runtime.AgentRole.IMPLEMENTER,
            namespace="main",
            service=service,
            role_session=role_session,
            provider_session_adapter=provider_session_adapter,
        )
    )

    with pytest.raises(Exception) as excinfo:
        asyncio.run(
            runtime.run_resident_prompt(
                runner=execution_adapter,
                request=runtime.ResidentRunRequest(
                    name="Runtime Consumer",
                    prompt="Continue from prior context.",
                    worktree=runtime.WorktreeMount(tmp_path),
                    model="gpt-5.4",
                    effort="medium",
                    session_plan=plan,
                ),
            )
        )

    assert type(excinfo.value).__name__ == "ManagedWorktreeMountPreconditionError"
    assert "Runtime Consumer" in str(excinfo.value)
    assert "role 'implementer'" in str(excinfo.value)
    assert service.build_env_state_dir_args == []
    assert provider_session_adapter.prepare_calls == []
