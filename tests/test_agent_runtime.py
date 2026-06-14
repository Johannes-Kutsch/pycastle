import asyncio
import json
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict, cast
from unittest.mock import MagicMock

import pytest

from pycastle.agents._work_invocation import (
    TextOutputAdapter,
    WorkExecutionAdapter,
    WorkInvocationDependencies,
    WorkInvocationRequest,
    invoke_work,
)
from pycastle.agents.runner import AgentRunner
from pycastle.agents.output_protocol import AgentOutput, AgentRole
from pycastle_agent_runtime.errors import (
    AgentCredentialFailureError,
    AgentTimeoutError,
    HardAgentError,
    RuntimeConfigurationError,
    TransientAgentError,
    UsageLimitError,
)
from pycastle_agent_runtime.work import reduce_text_output_events
from pycastle_agent_runtime.session import (
    ProviderSessionPreferences,
    ProviderSessionPreferencesRequest,
    ProviderSessionState,
    ProviderSessionStateRequest,
)
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


def _runtime_imported_application_modules(repo_root: Path) -> list[str]:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import json
                import sys

                import pycastle_agent_runtime as runtime

                runtime.ChainEntry
                runtime.ContinueNow
                runtime.ServiceRegistry
                runtime.ProviderSessionState
                runtime.ProviderSessionStateRequest
                runtime.RunKind
                runtime.StageOverride
                runtime.Stop
                runtime.UsageLimitOutcome
                runtime.decide_usage_limit_continuation
                runtime.select_configured_candidate_chain

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
                        name == prefix or name.startswith(f"{prefix}.")
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


def _runtime_attr_imported_application_modules(
    repo_root: Path, attr_name: str
) -> list[str]:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""
                import json
                import sys

                import pycastle_agent_runtime as runtime

                getattr(runtime, {attr_name!r})

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


def _standalone_runtime_attr_access_result(
    repo_root: Path, attr_name: str
) -> dict[str, str]:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""
                import importlib.abc
                import json
                import sys

                class _BlockPycastle(importlib.abc.MetaPathFinder):
                    def find_spec(self, fullname, path=None, target=None):
                        del path, target
                        if fullname == "pycastle" or fullname.startswith("pycastle."):
                            raise ModuleNotFoundError(
                                f"blocked test import: {{fullname}}"
                            )
                        return None

                sys.meta_path.insert(0, _BlockPycastle())

                import pycastle_agent_runtime as runtime

                try:
                    getattr(runtime, {attr_name!r})
                except Exception as exc:  # pragma: no cover - subprocess assertion surface
                    print(
                        json.dumps(
                            {{
                                "type": type(exc).__name__,
                                "message": str(exc),
                            }}
                        )
                    )
                else:
                    print(json.dumps({{"type": "ok", "message": ""}}))
                """
            ),
        ],
        cwd=repo_root,
        capture_output=True,
        check=True,
        text=True,
    )
    return json.loads(result.stdout)


def _standalone_runtime_attr_access_results(
    repo_root: Path, attr_names: list[str]
) -> dict[str, dict[str, str]]:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""
                import importlib.abc
                import json
                import sys

                class _BlockPycastle(importlib.abc.MetaPathFinder):
                    def find_spec(self, fullname, path=None, target=None):
                        del path, target
                        if fullname == "pycastle" or fullname.startswith("pycastle."):
                            raise ModuleNotFoundError(
                                f"blocked test import: {{fullname}}"
                            )
                        return None

                sys.meta_path.insert(0, _BlockPycastle())

                import pycastle_agent_runtime as runtime

                results = {{}}
                for attr_name in {attr_names!r}:
                    try:
                        getattr(runtime, attr_name)
                    except Exception as exc:  # pragma: no cover - subprocess surface
                        results[attr_name] = {{
                            "type": type(exc).__name__,
                            "message": str(exc),
                        }}
                    else:
                        results[attr_name] = {{"type": "ok", "message": ""}}

                print(json.dumps(results, sort_keys=True))
                """
            ),
        ],
        cwd=repo_root,
        capture_output=True,
        check=True,
        text=True,
    )
    return json.loads(result.stdout)


def _standalone_runtime_surface_behavior_result(
    repo_root: Path,
) -> dict[str, object]:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import importlib.abc
                import json
                import sys
                from datetime import datetime, timezone
                from pathlib import Path

                class _BlockPycastle(importlib.abc.MetaPathFinder):
                    def find_spec(self, fullname, path=None, target=None):
                        del path, target
                        if fullname == "pycastle" or fullname.startswith("pycastle."):
                            raise ModuleNotFoundError(
                                f"blocked test import: {fullname}"
                            )
                        return None

                sys.meta_path.insert(0, _BlockPycastle())

                import pycastle_agent_runtime as runtime

                class _Service:
                    name = "codex"

                    def is_available(self, now=None):
                        del now
                        return True

                    def next_wake_time(self):
                        return datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)

                    def mark_exhausted(self, reset_time):
                        del reset_time

                    def state_dir_relpath(self, role, namespace=""):
                        if namespace:
                            return f"{role.value}/{namespace}/{self.name}/"
                        return f"{role.value}/{self.name}/"

                    def is_resumable(self, state_dir):
                        del state_dir
                        return False

                    def provider_session_preferences(self, request):
                        del request
                        return runtime.ProviderSessionPreferences()

                    def provider_session_state(self, request):
                        return runtime.ProviderSessionState(
                            runtime.RunKind.FRESH,
                            None,
                            state_dir_relpath=request.state_dir_relpath,
                            state_dir_path=request.provider_state_dir,
                        )

                    def valid_efforts(self):
                        return frozenset({"medium"})

                    def valid_models(self):
                        return frozenset({"gpt-5.4"})

                class _RoleSession:
                    def session_uuid(self):
                        return "role-session"

                    def service_session_id(self, service_name):
                        del service_name
                        return None

                    def save_service_session_id(self, service_name, session_id):
                        del service_name, session_id

                    def service_session_metadata(self, service_name):
                        del service_name
                        return None

                    def exact_transcript_service_name(self):
                        return None

                    def record_successful_provider_session_metadata(
                        self,
                        service_name,
                        provider_session_id,
                    ):
                        del service_name, provider_session_id

                service = _Service()
                registry = runtime.ServiceRegistry({"codex": service})
                override = runtime.StageOverride(
                    service="missing",
                    model="ignored",
                    effort="medium",
                    fallback=runtime.StageOverride(
                        service="codex",
                        model="gpt-5.4",
                        effort="medium",
                    ),
                )
                resolved = registry.resolve(
                    override,
                    datetime(2026, 5, 17, 11, 0, tzinfo=timezone.utc),
                )
                selection = runtime.select_configured_candidate_chain(
                    override,
                    configured_service_names=("codex",),
                    available_service_names=("codex",),
                )
                provider_request = runtime.ProviderSessionStateRequest(
                    role_session=_RoleSession(),
                    provider_state_dir=None,
                    has_resumable_provider_state=False,
                )
                provider_state = service.provider_session_state(provider_request)
                plan = runtime.plan_provider_run_state(
                    runtime.ProviderRunStatePlanRequest(
                        worktree=Path("."),
                        role=runtime.AgentRole.IMPLEMENTER,
                        namespace="main",
                        service=service,
                        role_session=_RoleSession(),
                    )
                )
                decision = runtime.decide_usage_limit_continuation(
                    runtime.UsageLimitOutcome(),
                    stage_override=resolved,
                    service_registry=registry,
                    now=datetime(2026, 5, 17, 11, 0, tzinfo=timezone.utc),
                    compute_wake_time=lambda reset_time, now: (
                        now,
                        reset_time is None,
                    ),
                )
                failure = runtime.AgentFailedError(
                    role_value="implementer",
                    worktree_path=Path("."),
                    namespace="main",
                    service_name="codex",
                )
                print(
                    json.dumps(
                        {
                            "resolved_service": resolved.service,
                            "selected_chain": (
                                None
                                if selection.selected_chain is None
                                else runtime.render_chain_label(
                                    selection.selected_chain
                                )
                            ),
                            "provider_run_kind": provider_state.run_kind.value,
                            "planned_run_kind": plan.run_kind.value,
                            "planned_relpath": plan.provider_state_dir_relpath,
                            "usage_decision_type": type(decision).__name__,
                            "failure_session_dir": failure.session_dir,
                        },
                        sort_keys=True,
                    )
                )
                """
            ),
        ],
        cwd=repo_root,
        capture_output=True,
        check=True,
        text=True,
    )
    return json.loads(result.stdout)


def _standalone_runtime_prompt_result(repo_root: Path) -> str:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import asyncio
                import importlib.abc
                import sys
                from pathlib import Path

                class _BlockPycastle(importlib.abc.MetaPathFinder):
                    def find_spec(self, fullname, path=None, target=None):
                        del path, target
                        if fullname == "pycastle" or fullname.startswith("pycastle."):
                            raise ModuleNotFoundError(
                                f"blocked test import: {fullname}"
                            )
                        return None

                sys.meta_path.insert(0, _BlockPycastle())

                import pycastle_agent_runtime as runtime
                from pycastle_agent_runtime.work import WorkInvocationDependencies

                class _Service:
                    name = "codex"

                    def is_available(self, now=None):
                        del now
                        return True

                    def next_wake_time(self):
                        raise AssertionError("unexpected fallback selection")

                    def mark_exhausted(self, reset_time):
                        del reset_time

                class _PreparedRunSession:
                    run_kind = runtime.RunKind.FRESH
                    provider_session_id = None

                    def record_provider_session_id(self, provider_session_id):
                        self.provider_session_id = provider_session_id

                    def record_successful_run(self):
                        return None

                class _PreparedSession:
                    provider_state_dir_container_path = None

                    def prepare_for_run(self):
                        return None

                    def initial_provider_run_session(self):
                        return _PreparedRunSession()

                    def resumable_provider_run_session(self):
                        return _PreparedRunSession()

                    def protocol_reprompt_provider_run_session(self):
                        return None

                class _Session:
                    def exec_simple(self, cmd):
                        raise AssertionError(f"unexpected container exec: {cmd}")

                    def __exit__(self, exc_type, exc, tb):
                        return None

                class _Runner:
                    async def setup(self, git_name, git_email, work_body=""):
                        del git_name, git_email, work_body

                    async def work_text(
                        self,
                        prompt,
                        *,
                        role=runtime.AgentRole.IMPLEMENTER,
                        tool_policy=runtime.ToolPolicy.FULL,
                        run_kind=runtime.RunKind.FRESH,
                        session_uuid=None,
                        on_provider_session_id=None,
                    ):
                        del role, tool_policy, run_kind, session_uuid
                        if on_provider_session_id is not None:
                            on_provider_session_id("provider-session")
                        return f"standalone:{prompt}"

                class _ExecutionAdapter:
                    def __init__(self):
                        self.service = _Service()

                    def resolve_service(self, service_name=""):
                        assert service_name == "codex"
                        return self.service

                    def build_work_dependencies(self, *, name, model, effort, service):
                        assert name == "Standalone Runtime"
                        assert model == "gpt-5.4-mini"
                        assert effort == "low"
                        assert service is self.service
                        return WorkInvocationDependencies(
                            container_workspace="/tmp/workspace",
                            timeout_retries=0,
                            stage_key_for_role=lambda role: role.value,
                            prepare_session=lambda **_kwargs: _PreparedSession(),
                            build_session=lambda *_args: _Session(),
                            build_runner=lambda *_args: _Runner(),
                            get_git_identity=lambda: ("Test User", "test@example.com"),
                        )

                request = runtime.PromptRunRequest(
                    name="Standalone Runtime",
                    prompt="already rendered prompt",
                    worktree=runtime.WorktreeMount(Path(".")),
                    override=runtime.StageOverride(
                        service="codex",
                        model="gpt-5.4-mini",
                        effort="low",
                    ),
                )
                result = asyncio.run(
                    runtime.run_prompt(
                        runner=_ExecutionAdapter(),
                        service_registry=runtime.ServiceRegistry(
                            {"codex": _ExecutionAdapter().service}
                        ),
                        request=request,
                    )
                )
                print(result)
                """
            ),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip().splitlines()[-1]


def _standalone_runtime_agent_log_result(
    repo_root: Path, effective_logs_dir: Path
) -> dict[str, object]:
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
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""
                import importlib.abc
                import json
                import os
                import sys
                import time
                from datetime import datetime, timezone
                from pathlib import Path

                class _BlockPycastle(importlib.abc.MetaPathFinder):
                    def find_spec(self, fullname, path=None, target=None):
                        del path, target
                        if fullname == "pycastle" or fullname.startswith("pycastle."):
                            raise ModuleNotFoundError(
                                f"blocked test import: {{fullname}}"
                            )
                        return None

                os.environ["TZ"] = {tz_name!r}
                time.tzset()
                sys.meta_path.insert(0, _BlockPycastle())

                from pycastle_agent_runtime import AgentInvocationLog, AgentRole, RunKind

                logs_dir = Path({str(effective_logs_dir)!r})
                log = AgentInvocationLog(
                    now_local=lambda: datetime(
                        2026, 5, 17, 14, 30, tzinfo=timezone.utc
                    ).astimezone()
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
    def __init__(self, provider_state: ProviderSessionState) -> None:
        self._provider_state = provider_state
        self.preferences_requests: list[ProviderSessionPreferencesRequest] = []
        self.state_requests: list[ProviderSessionStateRequest] = []
        self.prepare_calls: list[tuple[Path | None, object | None]] = []
        self.record_calls: list[tuple[str, Path | None]] = []

    @property
    def service_name(self) -> str:
        return "generic"

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
        auth_seed_action: object | None = None,
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
        self.prepare_session_calls: list[dict[str, object]] = []

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

        def _prepare_session(**kwargs: object) -> _PreparedRuntimeSessionStandIn:
            self.prepare_session_calls.append(dict(kwargs))
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
    import pycastle_agent_runtime as runtime

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
        worktree=runtime.WorktreeMount(tmp_path),
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


def test_runtime_package_text_output_reducer_raises_runtime_owned_usage_limit() -> None:
    with pytest.raises(UsageLimitError) as excinfo:
        reduce_text_output_events(
            (UsageLimit(reset_time=None, raw_message="rate limited"),),
            lambda _turn: None,
            provider="codex",
        )

    assert excinfo.value.raw_message == "rate limited"
    assert excinfo.value.provider == "codex"


def test_runtime_package_text_output_reducer_raises_runtime_owned_transient_error() -> (
    None
):
    with pytest.raises(TransientAgentError) as excinfo:
        reduce_text_output_events(
            (TransientError(status_code=529, raw_message="API Error: 529 Overloaded"),),
            lambda _turn: None,
            provider="codex",
        )

    assert str(excinfo.value) == "API Error: 529 Overloaded"
    assert excinfo.value.status_code == 529


def test_runtime_package_text_output_reducer_raises_runtime_owned_hard_error() -> None:
    with pytest.raises(HardAgentError) as excinfo:
        reduce_text_output_events(
            (
                HardError(
                    status_code=403,
                    raw_message="API Error: 403 Forbidden",
                    classification="permission_denied",
                ),
            ),
            lambda _turn: None,
            provider="codex",
        )

    assert str(excinfo.value) == "API Error: 403 Forbidden"
    assert excinfo.value.status_code == 403
    assert excinfo.value.service_name == "codex"
    assert excinfo.value.classification == "permission_denied"


def test_runtime_package_text_output_reducer_raises_runtime_owned_credential_failure() -> (
    None
):
    with pytest.raises(AgentCredentialFailureError) as excinfo:
        reduce_text_output_events(
            (
                CredentialFailure(
                    raw_message="credential failure from provider adapter",
                    service_name="codex",
                    classification="operator_actionable_credential_failure",
                    source_observations=(),
                    status_code=401,
                ),
            ),
            lambda _turn: None,
            provider="claude",
        )

    assert str(excinfo.value) == "credential failure from provider adapter"
    assert excinfo.value.status_code == 401
    assert excinfo.value.service_name == "codex"
    assert excinfo.value.classification == "operator_actionable_credential_failure"


def test_runtime_package_surface_import_keeps_application_ownership_unloaded() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    imported = _runtime_imported_application_modules(repo_root)

    assert imported == []


@pytest.mark.parametrize("attr_name", ["PromptRunRequest", "run_prompt"])
def test_runtime_prompt_surface_import_keeps_application_ownership_unloaded(
    attr_name: str,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]

    imported = _runtime_attr_imported_application_modules(repo_root, attr_name)

    assert imported == []


def test_runtime_orchestration_surface_is_not_exported_in_standalone_runtime() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    result = _standalone_runtime_attr_access_result(repo_root, "run")

    assert result == {
        "type": "AttributeError",
        "message": "module 'pycastle_agent_runtime' has no attribute 'run'",
    }


def test_runtime_package_prompt_entrypoint_runs_standalone_without_pycastle() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    result = _standalone_runtime_prompt_result(repo_root)

    assert result == "standalone:already rendered prompt"


def test_runtime_top_level_surface_is_accessible_standalone_without_pycastle() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    results = _standalone_runtime_attr_access_results(
        repo_root,
        [
            "PromptRunRequest",
            "PromptRunSession",
            "PromptRuntime",
            "WorktreeMount",
            "run_prompt",
            "CancellationToken",
            "TextOutputAdapter",
            "WorkInvocationDependencies",
            "WorkInvocationRequest",
            "invoke_work",
            "ServiceRegistry",
            "ChainEntry",
            "select_configured_candidate_chain",
            "ProviderSessionAdapter",
            "ProviderSessionRecordingStore",
            "ProviderStatePreparationAction",
            "ProviderSessionState",
            "ProviderSessionStateRequest",
            "RunKind",
            "ProviderRunStatePlan",
            "ProviderRunStatePlanRequest",
            "plan_provider_run_state",
            "ContinueNow",
            "SleepUntil",
            "Stop",
            "decide_usage_limit_continuation",
            "AgentRuntimeError",
            "RuntimeConfigurationError",
            "UsageLimitError",
            "AgentFailedError",
            "AgentInvocationLog",
            "LogicalAgentInvocationLog",
            "WorkInvocationLog",
        ],
    )

    assert results == {
        attr_name: {"type": "ok", "message": ""} for attr_name in results
    }


def test_runtime_surface_behaviors_run_standalone_without_pycastle() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    result = _standalone_runtime_surface_behavior_result(repo_root)

    assert result == {
        "failure_session_dir": "implementer/main/codex",
        "planned_relpath": "implementer/main/codex/",
        "planned_run_kind": "fresh",
        "provider_run_kind": "fresh",
        "resolved_service": "codex",
        "selected_chain": "codex",
        "usage_decision_type": "ContinueNow",
    }


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
    import pycastle_agent_runtime as runtime

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


def test_runtime_package_import_isolation_guardrail_reports_application_ownership() -> (
    None
):
    from pycastle_agent_runtime._import_isolation import assert_runtime_import_isolation

    with pytest.raises(ImportError) as excinfo:
        assert_runtime_import_isolation(
            importer="pycastle_agent_runtime",
            newly_loaded_modules=[
                "pycastle.other",
                "pycastle.session.resume",
                "pycastle.infrastructure.container_runner",
                "pycastle.session.resume",
            ],
        )

    assert str(excinfo.value) == (
        "pycastle_agent_runtime imported pycastle application modules during "
        "runtime package initialization: "
        "pycastle.infrastructure.container_runner, pycastle.session.resume. "
        "This violates the pycastle_agent_runtime package boundary."
    )


def test_runtime_package_import_isolation_guardrail_rejects_pycastle_services() -> None:
    from pycastle_agent_runtime._import_isolation import assert_runtime_import_isolation

    with pytest.raises(ImportError) as excinfo:
        assert_runtime_import_isolation(
            importer="pycastle_agent_runtime",
            newly_loaded_modules=[
                "pycastle.services",
                "pycastle.services.agent_service",
            ],
        )

    assert str(excinfo.value) == (
        "pycastle_agent_runtime imported pycastle application modules during "
        "runtime package initialization: "
        "pycastle.services, pycastle.services.agent_service. "
        "This violates the pycastle_agent_runtime package boundary."
    )


def test_runtime_public_errors_use_agent_runtime_error_without_pycastle_alias_export():
    import pycastle_agent_runtime as runtime

    assert not hasattr(runtime, "PycastleError")
    assert issubclass(runtime.RuntimeConfigurationError, runtime.AgentRuntimeError)
    assert issubclass(runtime.UsageLimitError, runtime.AgentRuntimeError)
    assert issubclass(runtime.TransientAgentError, runtime.AgentRuntimeError)
    assert issubclass(runtime.HardAgentError, runtime.AgentRuntimeError)
    assert issubclass(runtime.AgentFailedError, runtime.AgentRuntimeError)


def test_runtime_public_errors_do_not_default_missing_service_names_to_claude():
    from pycastle_agent_runtime.errors import AgentFailedError, HardAgentError

    hard_error = HardAgentError(message="provider rejected request", status_code=400)
    failed_error = AgentFailedError(
        role_value="implementer",
        worktree_path=Path("."),
    )

    assert hard_error.service_name == ""
    assert failed_error.service_name == ""
    assert failed_error.session_dir == "implementer"


def test_runtime_provider_state_relpath_normalizes_legacy_namespaced_layout(
    tmp_path: Path,
) -> None:
    from pycastle_agent_runtime.roles import AgentRole as RuntimeAgentRole
    from pycastle_agent_runtime.session import ProviderSessionState, RunKind
    from pycastle_agent_runtime.session_planning import (
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
    from pycastle_agent_runtime.errors import AgentFailedError
    from pycastle_agent_runtime.roles import AgentRole as RuntimeAgentRole
    from pycastle_agent_runtime.session import (
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
        provider_session_path=".runtime-session/reviewer/main/codex",
    )

    assert failure.session_dir == ".runtime-session/reviewer/main/codex"


def test_runtime_session_helpers_default_to_provider_neutral_relpaths():
    from pycastle_agent_runtime.roles import AgentRole as RuntimeAgentRole
    from pycastle_agent_runtime.session import (
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
    from pycastle_agent_runtime.session import RunKind
    from pycastle_agent_runtime.session_planning import (
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
    from pycastle_agent_runtime.session import (
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


def test_runtime_provider_state_plan_records_successful_run_metadata_through_role_session_interface() -> (
    None
):
    from pycastle_agent_runtime.session import RunKind
    from pycastle_agent_runtime.session_planning import (
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


def test_runtime_provider_session_adapter_plan_allows_execution_service_without_session_methods(
    tmp_path: Path,
) -> None:
    from pycastle_agent_runtime.roles import AgentRole as RuntimeAgentRole
    from pycastle_agent_runtime.session import RunKind
    from pycastle_agent_runtime.session_planning import (
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


def test_runtime_provider_session_adapter_handles_local_preparation_and_session_recording(
    tmp_path: Path,
) -> None:
    from pycastle_agent_runtime.roles import AgentRole as RuntimeAgentRole
    from pycastle_agent_runtime.session import RunKind
    from pycastle_agent_runtime.session_planning import (
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
    from pycastle_agent_runtime.session import (
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


def test_runtime_session_exact_resume_uses_injected_provider_identity_matcher(
    tmp_path: Path,
) -> None:
    from pycastle_agent_runtime.session import (
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
    from pycastle_agent_runtime.roles import AgentRole as RuntimeAgentRole
    from pycastle_agent_runtime.session import ProviderSessionState, RunKind
    from pycastle_agent_runtime.session_planning import (
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
        )
    )

    assert plan.auth_seeding_requirement is AuthSeedingRequirement.REQUIRED
    assert plan.auth_seed_action is not None
    assert plan.auth_seed_action.source == Path.home() / ".codex" / "auth.json"
    assert plan.auth_seed_action.destination == state_dir / "auth.json"


def test_runtime_provider_state_plan_preserves_provider_auth_seed_failure_policy(
    tmp_path: Path,
) -> None:
    from pycastle_agent_runtime.provider_errors import ProviderErrorObservation
    from pycastle_agent_runtime.roles import AgentRole as RuntimeAgentRole
    from pycastle_agent_runtime.session import ProviderSessionState, RunKind
    from pycastle_agent_runtime.session_planning import (
        AuthSeedingRequirement,
        LocalAuthSeedAction,
        ProviderRunStatePlanRequest,
        plan_provider_run_state,
    )

    state_dir = tmp_path / ".pycastle-session" / "implementer" / "generic"
    missing = tmp_path / "host" / "generic-creds.json"
    observation = ProviderErrorObservation(
        service_name="generic",
        raw_provider_text="Generic credentials missing on the host.",
        source_stream="pre-dispatch host check",
        status_code=403,
    )
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
                missing_source_observations=(observation,),
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
        )
    )

    with pytest.raises(AgentCredentialFailureError) as excinfo:
        plan.prepare_provider_state_dir()

    assert str(excinfo.value) == "Generic credentials missing on the host."
    assert excinfo.value.service_name == "generic"
    assert excinfo.value.status_code == 403
    assert excinfo.value.classification == "operator_actionable_credential_failure"
    assert excinfo.value.observations == (observation,)


def test_runtime_provider_state_plan_without_provider_auth_seed_policy_raises_file_not_found(
    tmp_path: Path,
) -> None:
    from pycastle_agent_runtime.roles import AgentRole as RuntimeAgentRole
    from pycastle_agent_runtime.session import ProviderSessionState, RunKind
    from pycastle_agent_runtime.session_planning import (
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
        )
    )

    with pytest.raises(FileNotFoundError) as excinfo:
        plan.prepare_provider_state_dir()

    assert excinfo.value.args == (missing,)


def test_runtime_provider_state_plan_keeps_selected_provider_state_dir_for_opencode_resume_without_container_override_policy(
    tmp_path: Path,
) -> None:
    from pycastle_agent_runtime.roles import AgentRole as RuntimeAgentRole
    from pycastle_agent_runtime.session import ProviderSessionState, RunKind
    from pycastle_agent_runtime.session_planning import (
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
    from pycastle_agent_runtime.roles import AgentRole as RuntimeAgentRole
    from pycastle_agent_runtime.session import ProviderSessionState, RunKind
    from pycastle_agent_runtime.session_planning import (
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
        )
    )

    assert plan.provider_state_dir_container_path(
        worktree=tmp_path,
        container_workspace="/workspace",
    ) == ("/workspace/.pycastle-session/implementer/generic/")


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
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client([]),
        service_registry={"codex": service},
    )
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
    import pycastle_agent_runtime as runtime

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
    import pycastle_agent_runtime as runtime

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
        {
            "mount_path": tmp_path,
            "role": AgentRole.IMPLEMENTER,
            "session_namespace": "issues",
            "service": service,
            "container_workspace": "/home/agent/workspace",
            "run_session_plan": run_session_plan,
        }
    ]


def test_runtime_package_prompt_entrypoint_fills_missing_credential_failure_service_name_from_selected_fallback(
    tmp_path: Path,
) -> None:
    import pycastle_agent_runtime as runtime

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
                observations=(),
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
    import pycastle_agent_runtime as runtime

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
                observations=(),
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
                        prepare_session=lambda **_kwargs: (
                            _PreparedRuntimeSessionStandIn()
                        ),
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
                        prepare_session=lambda **_kwargs: (
                            _PreparedRuntimeSessionStandIn()
                        ),
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
                observations=(),
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
                        prepare_session=lambda **_kwargs: (
                            _PreparedRuntimeSessionStandIn()
                        ),
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
                observations=(),
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
                        prepare_session=lambda **_kwargs: (
                            _PreparedRuntimeSessionStandIn()
                        ),
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
        worktree=runtime.WorktreeMount(tmp_path),
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
    assert (tmp_path / ".pycastle-session" / "implementer" / "codex").is_dir()

    [log_path] = list(tmp_path.glob("runtime-consumer-*.log"))
    log_text = log_path.read_text(encoding="utf-8")
    assert '"prompt": "Return the final answer only."' in log_text
    assert '"result":"runtime result"' in log_text


def test_runtime_package_ships_standalone_distribution_metadata() -> None:
    from importlib.resources import files

    metadata_path = files("pycastle_agent_runtime").joinpath("pyproject.toml")

    assert metadata_path.is_file() is True
