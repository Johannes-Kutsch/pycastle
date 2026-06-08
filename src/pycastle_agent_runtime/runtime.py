from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pycastle import _time as _time_module
from pycastle.agents.output_protocol import AgentRole
from pycastle.agents.result import CancellationToken
from pycastle.agents.runner import (
    _CONTAINER_WORKSPACE,
    _stage_key_for_role,
    AgentRunner,
)
from pycastle.agents.session_dispatch import (
    SessionDispatchRequest,
    prepare_agent_session,
)
from pycastle.config.types import StageOverride
from pycastle.display.status_display import ModelDisplayMetadata, PlainStatusDisplay
from pycastle.errors import (
    AgentCredentialFailureError,
    AgentTimeoutError,
    DockerError,
    HardAgentError,
    SetupPhaseError,
    TransientAgentError,
    UsageLimitError,
)
from pycastle.infrastructure.container_runner import ContainerRunner
from pycastle.iteration._rows import status_row
from pycastle.session.agent import RunSessionPlan
from pycastle.services.claude_service import ClaudeService
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
    status_display = request.status_display
    if status_display is None:
        status_display = PlainStatusDisplay()

    token = request.token if request.token is not None else CancellationToken()
    if token.is_cancelled:
        raise UsageLimitError(reset_time=None, stage_key=_stage_key_for_role(role))

    prepared_session = prepare_agent_session(
        SessionDispatchRequest(
            mount_path=request.mount_path,
            role=role,
            session_namespace=request.session_namespace,
            service=resolved_service,
            container_workspace=_CONTAINER_WORKSPACE,
            run_session_plan=request.run_session_plan,
        )
    )
    initial_attempt = True

    async with status_row(
        status_display,
        request.name,
        kind="agent",
        must_close=False,
        work_body=request.work_body,
        model_display=ModelDisplayMetadata(
            service=resolved_service.name,
            model=resolved_override.model,
            effort=resolved_override.effort,
        ),
    ):
        session = runner._build_session(
            request.mount_path,
            resolved_service,
            prepared_session.provider_state_dir_container_path,
        )
        container_runner = ContainerRunner(
            request.name,
            session,
            model=resolved_override.model,
            effort=resolved_override.effort,
            status_display=status_display,
            cfg=runner._cfg,
            service=resolved_service,
        )
        try:
            git_name = runner._git_service.get_user_name()
            git_email = runner._git_service.get_user_email()
            try:
                await container_runner.setup(git_name, git_email, request.work_body)
            except DockerError as exc:
                raise SetupPhaseError(role.value, str(exc)) from exc

            prepared_session.prepare_for_run()
            retries_left = runner._cfg.timeout_retries
            while True:
                try:
                    provider_run_session = (
                        prepared_session.initial_provider_run_session()
                        if initial_attempt
                        else prepared_session.resumable_provider_run_session()
                    )
                    result = await container_runner.work_text(
                        request.prompt,
                        role=role,
                        tool_policy=request.tool_policy,
                        run_kind=provider_run_session.run_kind,
                        session_uuid=provider_run_session.provider_session_id,
                        on_provider_session_id=(
                            provider_run_session.record_provider_session_id
                        ),
                    )
                    provider_run_session.record_successful_run()
                    return result
                except AgentTimeoutError:
                    if retries_left <= 0:
                        raise
                    restart_num = runner._cfg.timeout_retries - retries_left + 1
                    status_display.print(
                        request.name,
                        "Timeout — restarting"
                        f" (attempt {restart_num}/{runner._cfg.timeout_retries})",
                    )
                    retries_left -= 1
                    initial_attempt = False
                except UsageLimitError as err:
                    if err.stage_key is None:
                        err.stage_key = _stage_key_for_role(role)
                    if err.is_permanent and isinstance(resolved_service, ClaudeService):
                        err.account_label = (
                            resolved_service.mark_permanently_exhausted()
                        )
                    else:
                        resolved_service.mark_exhausted(err.reset_time)
                    token.cancel()
                    raise
                except TransientAgentError:
                    token.cancel()
                    raise
                except AgentCredentialFailureError as err:
                    token.cancel()
                    err.caller = request.name
                    err.service_name = resolved_service.name
                    raise
                except HardAgentError as err:
                    token.cancel()
                    err.caller = request.name
                    err.service_name = resolved_service.name
                    raise
        finally:
            try:
                session.__exit__(None, None, None)
            except Exception:
                pass
