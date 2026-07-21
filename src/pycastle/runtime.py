from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any

from . import _time as _time_module
from .agents.output_protocol import AgentRole
from .execution_contracts import (
    CancellationToken,
    PromptRunRequest,
    PromptRunSession,
    PromptRuntimeExecutionAdapter,
    PreparedProviderRunSession,
    PreparedRunSessionState,
    RuntimeInvocationRequest,
    RuntimeModelDisplayMetadata,
    RuntimeRunSession,
    TextOutputAdapter,
    WorktreeMount,
)
from .services.runtime_services import ToolPolicy
from .services.service_registry import ServiceRegistry
from .runtime_session import RunKind
from .session_planning import ResidentSessionPlan
from .config.types import StageOverride
from .stage_priority_chain import iter_stage_chain

__all__ = [
    "OneShotRunRequest",
    "OneShotRunResult",
    "OneShotRuntime",
    "OneShotRuntimeExecutionAdapter",
    "OneShotRuntimeMetadata",
    "ResidentRunRequest",
    "ResidentRunResult",
    "ResidentRuntime",
    "ResidentRuntimeExecutionAdapter",
    "ResidentRuntimeMetadata",
    "PromptRunRequest",
    "PromptRunSession",
    "PromptRuntime",
    "PromptRuntimeExecutionAdapter",
    "ToolPolicy",
    "WorktreeMount",
    "run_one_shot",
    "run_prompt",
    "run_resident_prompt",
]

OneShotRunRequest = PromptRunRequest
OneShotRuntimeExecutionAdapter = PromptRuntimeExecutionAdapter
ResidentRuntimeExecutionAdapter = PromptRuntimeExecutionAdapter


@dataclasses.dataclass(frozen=True)
class OneShotRuntimeMetadata:
    provider_session_id: str | None
    run_kind: RunKind
    session_namespace: str


@dataclasses.dataclass(frozen=True)
class OneShotRunResult:
    selected_service: str
    selected_model: str
    selected_effort: str
    used_fallback: bool
    selected_service_path: tuple[str, ...]
    raw_output: Any
    runtime_metadata: OneShotRuntimeMetadata


@dataclasses.dataclass(frozen=True)
class ResidentRuntimeMetadata:
    service_name: str
    provider_session_id: str | None
    run_kind: RunKind
    session_namespace: str
    exact_transcript_match: bool


@dataclasses.dataclass(frozen=True)
class ResidentRunResult:
    output: str
    runtime_metadata: ResidentRuntimeMetadata


@dataclasses.dataclass(frozen=True)
class ResidentRunRequest:
    prompt: str
    worktree: WorktreeMount
    model: str
    effort: str
    session_plan: ResidentSessionPlan
    tool_policy: ToolPolicy = ToolPolicy.FULL
    name: str = "Runtime Agent"
    status_display: Any = None
    work_body: str = ""
    token: CancellationToken | None = None

    @property
    def mount_path(self) -> Any:
        return self.worktree.host_path


@dataclasses.dataclass
class _ResidentPreparedProviderRunSession:
    run_kind: RunKind
    provider_session_id: str | None
    _session_plan: ResidentSessionPlan = dataclasses.field(repr=False)

    def record_provider_session_id(self, provider_session_id: str) -> None:
        self.provider_session_id = provider_session_id
        self._session_plan.record_provider_session_id(provider_session_id)

    def record_successful_run(self) -> None:
        self._session_plan.record_successful_run()


@dataclasses.dataclass
class _ResidentPreparedRuntimeState(PreparedRunSessionState):
    session_plan: ResidentSessionPlan
    provider_state_dir_container_path: str | None
    _initial_session: _ResidentPreparedProviderRunSession = dataclasses.field(
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self._initial_session = _ResidentPreparedProviderRunSession(
            run_kind=self.session_plan.run_kind,
            provider_session_id=self.session_plan.provider_session_id,
            _session_plan=self.session_plan,
        )

    @property
    def provider_session_id(self) -> str | None:
        return self._initial_session.provider_session_id

    def prepare_for_run(self) -> None:
        self.session_plan.prepare_provider_state_dir()
        self._initial_session.provider_session_id = (
            self.session_plan.prepared_provider_session_id()
        )

    def initial_provider_run_session(self) -> PreparedProviderRunSession:
        return self._initial_session

    def resumable_provider_run_session(self) -> PreparedProviderRunSession:
        return self._initial_session

    def protocol_reprompt_provider_run_session(
        self,
    ) -> PreparedProviderRunSession | None:
        return None


def _selected_service_path(
    override: StageOverride,
    *,
    selected_service: str,
) -> tuple[str, ...]:
    path: list[str] = []
    for node in iter_stage_chain(override):
        if not node.service:
            continue
        path.append(node.service)
        if node.service == selected_service:
            return tuple(path)
    return (selected_service,)


def _require_execution_adapter_method(
    adapter: PromptRuntimeExecutionAdapter,
    method_name: str,
) -> Any:
    method = getattr(adapter, method_name, None)
    if callable(method):
        return method
    from .errors import RuntimeConfigurationError

    raise RuntimeConfigurationError(
        f"Prompt runtime requires an execution adapter with callable `{method_name}()`."
    )


class PromptRuntime:
    def __init__(
        self,
        *,
        execution_adapter: PromptRuntimeExecutionAdapter,
        service_registry: ServiceRegistry | dict[str, Any] | None = None,
    ) -> None:
        registry = (
            service_registry
            if isinstance(service_registry, ServiceRegistry)
            else ServiceRegistry(service_registry or {})
        )
        self._service_registry = registry
        self._execution_adapter = execution_adapter

    async def run_prompt(self, request: PromptRunRequest) -> str:
        return await run_prompt(
            runner=self._execution_adapter,
            service_registry=self._service_registry,
            request=request,
        )


class _OneShotOutputAdapter:
    def __init__(self, *, prompt: str, session_namespace: str) -> None:
        self._prompt = prompt
        self._session_namespace = session_namespace
        self.runtime_metadata = OneShotRuntimeMetadata(
            provider_session_id=None,
            run_kind=RunKind.FRESH,
            session_namespace=session_namespace,
        )

    async def build_prompt(
        self,
        *,
        run_kind: RunKind,
        container_exec: Any,
    ) -> str:
        del run_kind, container_exec
        return self._prompt

    async def invoke(
        self,
        *,
        runner: Any,
        role: AgentRole,
        prompt: str,
        run_kind: RunKind,
        session_uuid: str | None,
        on_provider_session_id: Any,
    ) -> Any:
        provider_session_id: str | None = None

        def _record_provider_session_id(value: str) -> None:
            nonlocal provider_session_id
            provider_session_id = value
            on_provider_session_id(value)

        raw_output = await runner.work(
            role,
            prompt,
            run_kind=run_kind,
            session_uuid=session_uuid,
            on_provider_session_id=_record_provider_session_id,
        )
        self.runtime_metadata = OneShotRuntimeMetadata(
            provider_session_id=provider_session_id or session_uuid,
            run_kind=run_kind,
            session_namespace=self._session_namespace,
        )
        return raw_output

    def is_successful_result(self, result: Any) -> bool:
        del result
        return True

    def protocol_reprompt_message(self) -> str | None:
        return None

    def protocol_error_result(self) -> Any | None:
        return None

    def non_typed_failure_result(self) -> Any | None:
        return None

    def protocol_error_types(self) -> tuple[type[BaseException], ...]:
        return ()

    def finalize_result(
        self,
        result: Any,
        *,
        role: AgentRole,
        mount_path: Any,
        session_namespace: str,
        service_name: str,
        invocation_log_path: Path | str | None = None,
    ) -> Any:
        del role, mount_path, session_namespace, service_name, invocation_log_path
        return result


class OneShotRuntime:
    def __init__(
        self,
        *,
        execution_adapter: OneShotRuntimeExecutionAdapter,
        service_registry: ServiceRegistry | dict[str, Any] | None = None,
    ) -> None:
        registry = (
            service_registry
            if isinstance(service_registry, ServiceRegistry)
            else ServiceRegistry(service_registry or {})
        )
        self._service_registry = registry
        self._execution_adapter = execution_adapter

    async def run_one_shot(self, request: OneShotRunRequest) -> OneShotRunResult:
        return await run_one_shot(
            runner=self._execution_adapter,
            service_registry=self._service_registry,
            request=request,
        )


class ResidentRuntime:
    def __init__(
        self,
        *,
        execution_adapter: ResidentRuntimeExecutionAdapter,
    ) -> None:
        self._execution_adapter = execution_adapter

    async def run_resident_prompt(
        self,
        request: ResidentRunRequest,
    ) -> ResidentRunResult:
        return await run_resident_prompt(
            runner=self._execution_adapter,
            request=request,
        )


async def run_prompt(
    *,
    runner: PromptRuntimeExecutionAdapter,
    service_registry: ServiceRegistry,
    request: PromptRunRequest,
) -> str:
    resolved_override = service_registry.resolve(
        request.override,
        _time_module.now_local(),
    )
    role = AgentRole.IMPLEMENTER
    resolve_service = _require_execution_adapter_method(runner, "resolve_service")
    build_work_dependencies = _require_execution_adapter_method(
        runner,
        "build_work_dependencies",
    )
    resolved_service = resolve_service(resolved_override.service)
    dependencies = build_work_dependencies(
        name=request.name,
        model=resolved_override.model,
        effort=resolved_override.effort,
        service=resolved_service,
    )
    run_session = RuntimeRunSession(
        mount_path=request.mount_path,
        role=role,
        session_namespace=request.session_namespace,
        service=resolved_service,
        container_workspace=dependencies.container_workspace,
        run_session_plan=request.run_session_plan,
    )

    return await _execute_runtime_request(
        RuntimeInvocationRequest(
            name=request.name,
            mount_path=request.mount_path,
            role=role,
            service=resolved_service,
            model=resolved_override.model,
            effort=resolved_override.effort,
            output_adapter=TextOutputAdapter(
                prompt=request.prompt,
                tool_policy=request.tool_policy,
            ),
            dependencies=dependencies,
            status_display=request.status_display,
            token=request.token,
            work_body=request.work_body,
            run_session=run_session,
        )
    )


async def run_one_shot(
    *,
    runner: OneShotRuntimeExecutionAdapter,
    service_registry: ServiceRegistry,
    request: OneShotRunRequest,
) -> OneShotRunResult:
    from .errors import RuntimeConfigurationError, UsageLimitError

    if not service_registry.has_configured_candidate(request.override):
        raise RuntimeConfigurationError(
            "One-shot runtime requires at least one configured service candidate."
        )

    role = AgentRole.IMPLEMENTER
    resolve_service = _require_execution_adapter_method(runner, "resolve_service")
    build_work_dependencies = _require_execution_adapter_method(
        runner,
        "build_work_dependencies",
    )

    while True:
        now = _time_module.now_local()
        if request.token is not None and request.token.is_cancelled:
            raise UsageLimitError(
                reset_time=None,
                stage_key=role.value,
            )
        if not service_registry.has_available_for(request.override, now):
            resolved_override = service_registry.resolve(request.override, now)
            selected_service_name = resolved_override.service
            next_wake_time = service_registry.next_wake_time_for(
                request.override,
                now,
            )
            raise UsageLimitError(
                reset_time=next_wake_time,
                provider=selected_service_name,
            )

        resolved_override = service_registry.resolve(
            request.override,
            now,
        )
        resolved_service = resolve_service(resolved_override.service)
        dependencies = build_work_dependencies(
            name=request.name,
            model=resolved_override.model,
            effort=resolved_override.effort,
            service=resolved_service,
        )
        run_session = RuntimeRunSession(
            mount_path=request.mount_path,
            role=role,
            session_namespace=request.session_namespace,
            service=resolved_service,
            container_workspace=dependencies.container_workspace,
            run_session_plan=request.run_session_plan,
        )
        output_adapter = _OneShotOutputAdapter(
            prompt=request.prompt,
            session_namespace=request.session_namespace,
        )
        attempt_token = (
            CancellationToken() if request.token is not None else request.token
        )
        try:
            raw_output = await _execute_runtime_request(
                RuntimeInvocationRequest(
                    name=request.name,
                    mount_path=request.mount_path,
                    role=role,
                    service=resolved_service,
                    model=resolved_override.model,
                    effort=resolved_override.effort,
                    output_adapter=output_adapter,
                    dependencies=dependencies,
                    status_display=request.status_display,
                    token=attempt_token,
                    work_body=request.work_body,
                    run_session=run_session,
                )
            )
        except Exception as exc:
            if isinstance(exc, UsageLimitError):
                continue
            raise

        selected_service_path = _selected_service_path(
            request.override,
            selected_service=resolved_service.name,
        )
        return OneShotRunResult(
            selected_service=resolved_service.name,
            selected_model=resolved_override.model,
            selected_effort=resolved_override.effort,
            used_fallback=len(selected_service_path) > 1,
            selected_service_path=selected_service_path,
            raw_output=raw_output,
            runtime_metadata=output_adapter.runtime_metadata,
        )


async def run_resident_prompt(
    *,
    runner: ResidentRuntimeExecutionAdapter,
    request: ResidentRunRequest,
) -> ResidentRunResult:
    build_work_dependencies = _require_execution_adapter_method(
        runner,
        "build_work_dependencies",
    )
    plan = request.session_plan
    dependencies = build_work_dependencies(
        name=request.name,
        model=request.model,
        effort=request.effort,
        service=plan.service,
    )
    prepared_session = _ResidentPreparedRuntimeState(
        session_plan=plan,
        provider_state_dir_container_path=plan.provider_state_dir_container_path(
            dependencies.container_workspace
        ),
    )
    resident_dependencies = dataclasses.replace(
        dependencies,
        prepare_session=lambda _run_session: prepared_session,
    )
    run_session = RuntimeRunSession(
        mount_path=plan.worktree,
        role=plan.role,
        session_namespace=plan.namespace,
        service=plan.service,
        container_workspace=dependencies.container_workspace,
        run_session_plan=plan,
    )
    output = await _execute_runtime_request(
        RuntimeInvocationRequest(
            name=request.name,
            mount_path=plan.worktree,
            role=plan.role,
            service=plan.service,
            model=request.model,
            effort=request.effort,
            output_adapter=TextOutputAdapter(
                prompt=request.prompt,
                tool_policy=request.tool_policy,
            ),
            dependencies=resident_dependencies,
            status_display=request.status_display,
            token=request.token,
            work_body=request.work_body,
            run_session=run_session,
        )
    )
    return ResidentRunResult(
        output=output,
        runtime_metadata=ResidentRuntimeMetadata(
            service_name=plan.service.name,
            provider_session_id=prepared_session.provider_session_id,
            run_kind=plan.run_kind,
            session_namespace=plan.namespace,
            exact_transcript_match=plan.exact_transcript_match,
        ),
    )


async def _execute_runtime_request(request: RuntimeInvocationRequest[Any]) -> Any:
    status_display = request.status_display
    if status_display is None:
        status_display = request.dependencies.status_display_factory()

    token = request.token if request.token is not None else CancellationToken()
    if token.is_cancelled:
        from .errors import UsageLimitError

        raise UsageLimitError(
            reset_time=None,
            stage_key=request.dependencies.stage_key_for_role(request.role),
        )

    validate_mount_preconditions = request.dependencies.validate_mount_preconditions
    if validate_mount_preconditions is not None:
        validate_mount_preconditions(request.name, request.mount_path, request.role)

    run_session = request.run_session
    assert run_session is not None
    prepared_session = request.dependencies.prepare_session(run_session)
    non_typed_retry_done = False
    initial_attempt = True

    async with request.dependencies.status_row_factory(
        status_display,
        request.name,
        kind="agent",
        must_close=False,
        work_body=request.work_body,
        color_key=request.color_key,
        model_display=_build_model_display_metadata(request),
    ) as row:
        session = request.dependencies.build_session(
            request.mount_path,
            request.service,
            prepared_session.provider_state_dir_container_path,
        )
        runner = request.dependencies.build_runner(
            session, status_display, request.mount_path
        )
        try:
            git_name, git_email = request.dependencies.get_git_identity()
            await runner.setup(git_name, git_email, request.work_body)
            prepared_session.prepare_for_run()
            loop = asyncio.get_running_loop()

            async def container_exec(cmd: str) -> str:
                return await loop.run_in_executor(None, session.exec_simple, cmd)

            retries_left = request.dependencies.timeout_retries
            while True:
                provider_run_session = (
                    prepared_session.initial_provider_run_session()
                    if initial_attempt
                    else prepared_session.resumable_provider_run_session()
                )
                try:
                    prompt = await request.output_adapter.build_prompt(
                        run_kind=provider_run_session.run_kind,
                        container_exec=container_exec,
                    )
                    result, successful_run_session = await _execute_runtime_attempt(
                        request=request,
                        row=row,
                        prepared_session=prepared_session,
                        runner=runner,
                        prompt=prompt,
                        provider_run_session=provider_run_session,
                    )
                    if request.output_adapter.is_successful_result(result):
                        successful_run_session.record_successful_run()
                    else:
                        row.close("failed", shutdown_style="error")
                    return request.output_adapter.finalize_result(
                        result,
                        role=request.role,
                        mount_path=request.mount_path,
                        session_namespace=request.session_namespace,
                        service_name=request.service.name,
                        invocation_log_path=getattr(runner, "log_path", None),
                    )
                except Exception as err:
                    from agent_runtime.errors import (
                        AgentCredentialFailureError,
                        HardAgentError,
                    )
                    from .errors import (
                        AgentTimeoutError,
                        TransientAgentError,
                        UsageLimitError,
                    )

                    if isinstance(err, AgentTimeoutError):
                        if not err.role_value:
                            err.role_value = request.role.value
                            err.worktree_path = request.mount_path
                        if retries_left <= 0:
                            raise
                        retries_left -= 1
                        initial_attempt = False
                        continue
                    if isinstance(err, UsageLimitError):
                        if err.stage_key is None:
                            err.stage_key = request.dependencies.stage_key_for_role(
                                request.role
                            )
                        request.dependencies.handle_provider_account_exhaustion(
                            request.service,
                            err,
                        )
                        token.cancel()
                        raise
                    if isinstance(err, TransientAgentError):
                        token.cancel()
                        if request.dependencies.transient_status_message is not None:
                            status_display.print(
                                request.name,
                                request.dependencies.transient_status_message(err),
                            )
                        raise
                    if isinstance(err, AgentCredentialFailureError):
                        token.cancel()
                        err.caller = request.name
                        if not err.service_name:
                            err.service_name = request.service.name
                        raise
                    if isinstance(err, HardAgentError):
                        token.cancel()
                        err.caller = request.name
                        err.service_name = request.service.name
                        raise
                    if (
                        not request.allow_non_typed_resume_retry
                        or provider_run_session.run_kind != RunKind.RESUME
                    ):
                        raise
                    failure_result = request.output_adapter.non_typed_failure_result()
                    if failure_result is None:
                        raise
                    if non_typed_retry_done:
                        row.close("failed", shutdown_style="error")
                        return request.output_adapter.finalize_result(
                            failure_result,
                            role=request.role,
                            mount_path=request.mount_path,
                            session_namespace=request.session_namespace,
                            service_name=request.service.name,
                            invocation_log_path=getattr(runner, "log_path", None),
                        )
                    non_typed_retry_done = True
        finally:
            try:
                session.__exit__(None, None, None)
            except Exception:
                pass


async def _execute_runtime_attempt(
    *,
    request: RuntimeInvocationRequest[Any],
    row: Any,
    prepared_session: PreparedRunSessionState,
    runner: Any,
    prompt: str,
    provider_run_session: PreparedProviderRunSession,
) -> tuple[Any, PreparedProviderRunSession]:
    reprompt_message = request.output_adapter.protocol_reprompt_message()
    protocol_error_result = request.output_adapter.protocol_error_result()
    protocol_error_types = request.output_adapter.protocol_error_types()
    max_attempts = (
        3 if reprompt_message is not None and protocol_error_result is not None else 1
    )
    work_prompt = prompt
    work_run_session = provider_run_session
    for _ in range(max_attempts):
        try:
            result = await request.output_adapter.invoke(
                runner=runner,
                role=request.role,
                prompt=work_prompt,
                run_kind=work_run_session.run_kind,
                session_uuid=work_run_session.provider_session_id,
                on_provider_session_id=work_run_session.record_provider_session_id,
            )
            return result, work_run_session
        except Exception as exc:
            if not protocol_error_types or not isinstance(exc, protocol_error_types):
                raise
            if reprompt_message is None or protocol_error_result is None:
                raise
            next_run_session = prepared_session.protocol_reprompt_provider_run_session()
            if next_run_session is None:
                row.close("failed", shutdown_style="error")
                return protocol_error_result, work_run_session
            latest_reprompt_message = request.output_adapter.protocol_reprompt_message()
            if latest_reprompt_message is None:
                raise
            work_prompt = latest_reprompt_message
            work_run_session = next_run_session
    row.close("failed", shutdown_style="error")
    assert protocol_error_result is not None
    return protocol_error_result, work_run_session


def _build_model_display_metadata(
    request: RuntimeInvocationRequest[Any],
) -> RuntimeModelDisplayMetadata | None:
    if request.dependencies.build_model_display_metadata is None:
        return RuntimeModelDisplayMetadata(
            service=request.service.name,
            model=request.model,
            effort=request.effort,
        )
    return request.dependencies.build_model_display_metadata(
        request.service.name,
        request.model,
        request.effort,
    )
