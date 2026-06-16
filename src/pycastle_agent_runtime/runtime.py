from __future__ import annotations

import dataclasses
from typing import Any

from . import _time as _time_module
from .contracts import ToolPolicy
from .execution_contracts import (
    CancellationToken,
    PromptRunRequest,
    PromptRunSession,
    PromptRuntimeExecutionAdapter,
    RunSessionPlan,
    TextOutputAdapter,
    WorkInvocationRequest,
    WorktreeMount,
)
from .errors import RuntimeConfigurationError, UsageLimitError
from .roles import AgentRole
from .service_registry import ServiceRegistry
from .session import RunKind
from .stage_priority_chain import iter_stage_chain
from .types import StageOverride
from .work import invoke_work

__all__ = [
    "OneShotRunRequest",
    "OneShotRunResult",
    "OneShotRuntime",
    "OneShotRuntimeExecutionAdapter",
    "OneShotRuntimeMetadata",
    "PromptRunRequest",
    "PromptRunSession",
    "PromptRuntime",
    "PromptRuntimeExecutionAdapter",
    "ToolPolicy",
    "WorktreeMount",
    "run_one_shot",
    "run_prompt",
]

OneShotRunRequest = PromptRunRequest
OneShotRuntimeExecutionAdapter = PromptRuntimeExecutionAdapter


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
    ) -> Any:
        del role, mount_path, session_namespace, service_name
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
    run_session = RunSessionPlan(
        mount_path=request.mount_path,
        role=role,
        session_namespace=request.session_namespace,
        service=resolved_service,
        container_workspace=dependencies.container_workspace,
        run_session_plan=request.run_session_plan,
    )

    return await invoke_work(
        WorkInvocationRequest(
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
        run_session = RunSessionPlan(
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
            raw_output = await invoke_work(
                WorkInvocationRequest(
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
