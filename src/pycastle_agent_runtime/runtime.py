from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pycastle import _time as _time_module
from pycastle.agents._work_invocation import (
    TextOutputAdapter,
    WorkInvocationDependencies,
    WorkInvocationRequest,
    invoke_work,
)
from pycastle.agents.result import CancellationToken
from pycastle.agents.runner import AgentRunner
from pycastle.session.agent import RunSessionPlan
from pycastle.services.agent_service import AgentService
from pycastle.services.flag_profiles import AgentToolPolicyGroup

from .roles import AgentRole
from .service_registry import ServiceRegistry
from .types import StageOverride


ToolPolicy = AgentToolPolicyGroup


class PromptRuntimeExecutionAdapter(Protocol):
    def resolve_service(self, service_name: str = "") -> AgentService: ...

    def build_work_dependencies(
        self,
        *,
        name: str,
        model: str,
        effort: str,
        service: AgentService,
    ) -> WorkInvocationDependencies: ...


@dataclass(frozen=True)
class PromptRunRequest:
    prompt: str
    mount_path: Path
    override: StageOverride
    tool_policy: ToolPolicy = ToolPolicy.FULL
    name: str = "Runtime Agent"
    status_display: Any = None
    work_body: str = ""
    token: CancellationToken | None = None
    session_namespace: str = ""
    run_session_plan: RunSessionPlan | None = None


class PromptRuntime:
    def __init__(
        self,
        *,
        env: dict[str, str],
        cfg: Any,
        git_service: Any,
        docker_client: Any = None,
        service_registry: ServiceRegistry | dict[str, Any] | None = None,
    ) -> None:
        registry = (
            service_registry
            if isinstance(service_registry, ServiceRegistry)
            else ServiceRegistry(service_registry or {})
        )
        self._service_registry = registry
        self._runner = AgentRunner(
            env,
            cfg,
            git_service,
            docker_client=docker_client,
            service_registry=registry.services,
        )

    async def run_prompt(self, request: PromptRunRequest) -> str:
        return await run_prompt(
            runner=self._runner,
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
    resolved_service = runner.resolve_service(resolved_override.service)
    dependencies = runner.build_work_dependencies(
        name=request.name,
        model=resolved_override.model,
        effort=resolved_override.effort,
        service=resolved_service,
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
            session_namespace=request.session_namespace,
            run_session_plan=request.run_session_plan,
        )
    )
