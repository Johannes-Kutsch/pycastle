from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pycastle import _time as _time_module
from pycastle.agents._work_invocation import (
    TextOutputAdapter,
    WorkInvocationDependencies,
    WorkInvocationRequest,
    format_transient_status_message,
    invoke_work,
)
from pycastle.agents.output_protocol import AgentRole
from pycastle.agents.result import CancellationToken
from pycastle.agents.runner import (
    _CONTAINER_WORKSPACE,
    _stage_key_for_role,
    AgentRunner,
)
from pycastle.config.types import StageOverride
from pycastle.infrastructure.container_runner import ContainerRunner
from pycastle.session.agent import RunSessionPlan
from pycastle.services.flag_profiles import AgentToolPolicyGroup

from .service_registry import ServiceRegistry


ToolPolicy = AgentToolPolicyGroup


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
    runner: AgentRunner,
    service_registry: ServiceRegistry,
    request: PromptRunRequest,
) -> str:
    resolved_override = service_registry.resolve(
        request.override,
        _time_module.now_local(),
    )
    role = AgentRole.IMPLEMENTER
    resolved_service = runner._resolve_service(resolved_override.service)
    dependencies = WorkInvocationDependencies(
        container_workspace=_CONTAINER_WORKSPACE,
        timeout_retries=runner._cfg.timeout_retries,
        stage_key_for_role=_stage_key_for_role,
        build_session=runner._build_session,
        build_runner=lambda session, status_display: ContainerRunner(
            request.name,
            session,
            model=resolved_override.model,
            effort=resolved_override.effort,
            status_display=status_display,
            cfg=runner._cfg,
            service=resolved_service,
        ),
        get_git_identity=lambda: (
            runner._git_service.get_user_name(),
            runner._git_service.get_user_email(),
        ),
        transient_status_message=format_transient_status_message,
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
