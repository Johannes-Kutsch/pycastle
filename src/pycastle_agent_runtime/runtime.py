from __future__ import annotations

from typing import Any

from . import _time as _time_module
from .contracts import ToolPolicy
from .execution_contracts import (
    PromptRunRequest,
    PromptRunSession,
    PromptRuntimeExecutionAdapter,
    RunSessionPlan,
    TextOutputAdapter,
    WorkInvocationRequest,
    WorktreeMount,
)
from .errors import RuntimeConfigurationError
from .roles import AgentRole
from .service_registry import ServiceRegistry
from .work import invoke_work

__all__ = [
    "PromptRunRequest",
    "PromptRunSession",
    "PromptRuntime",
    "PromptRuntimeExecutionAdapter",
    "ToolPolicy",
    "WorktreeMount",
    "run_prompt",
]


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
