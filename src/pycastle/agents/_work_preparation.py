from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pycastle import stage_registry
from pycastle.agents import protocol_reprompt
from pycastle.agents.output_protocol import AgentRole
from pycastle.display.status_display import (
    ModelDisplayMetadata,
    PlainStatusDisplay,
    StatusDisplay,
)
from pycastle.errors import UsageLimitError
from pycastle.execution_contracts import CancellationToken
from pycastle.infrastructure.container_runner import (
    ContainerRunner,
    _ContainerRunnerConfig,
)
from pycastle.prompts.dispatch import render_prompt_invocation
from pycastle.session import RoleSession, RunKind

if TYPE_CHECKING:
    from collections.abc import Callable

    from pycastle.agents.runner import RunRequest
    from pycastle.config import Config
    from pycastle.prompts.pipeline import PromptRenderer
    from pycastle.services import GitService
    from pycastle.services.runtime_services import AgentService

_CONTAINER_WORKSPACE = "/home/agent/workspace"


def _default_effort() -> str:
    return "medium"


def _default_model(service: AgentService) -> str:
    valid_models = service.valid_models()
    for candidate in ("gpt-5.5", "gpt-5.4", "haiku", "opus", "sonnet"):
        if candidate in valid_models:
            return candidate
    if valid_models:
        return min(valid_models)
    return "gpt-5.5"


@dataclasses.dataclass(frozen=True)
class _WorkPreparation:
    role_session: RoleSession
    provider_state_dir: Path
    provider_auth: Any
    resolved_model: str
    resolved_effort: str
    runner: ContainerRunner
    runtime_client: Any
    model_display: ModelDisplayMetadata
    color_key: int | None
    planned_protocol_reprompt: Callable[[str | None], str]
    render_prompt: Any  # Callable[[RunRequest, RunKind], Awaitable[str]]
    status_display: StatusDisplay
    session: Any  # DockerSession or _UnavailableDockerSession
    git_name: str
    git_email: str
    service: AgentService


def _prepare_work(
    request: RunRequest,
    service: AgentService,
    cfg: Config,
    git_service: GitService,
    renderer: PromptRenderer,
    build_session: Callable[..., Any],
) -> _WorkPreparation:
    token = request.token if request.token is not None else CancellationToken()
    if token.is_cancelled or not service.is_available():
        raise UsageLimitError(
            reset_time=None,
            stage_key=stage_registry.stage_key_for_role(request.role),
        )

    status_display: StatusDisplay = (
        request.status_display
        if request.status_display is not None
        else PlainStatusDisplay()
    )

    role_session = RoleSession(
        request.mount_path, request.role, request.session_namespace
    )

    state_dir_relpath = service.state_dir_relpath(
        request.role, request.session_namespace
    )
    if state_dir_relpath is not None:
        provider_state_dir: Path = request.mount_path / state_dir_relpath
        state_dir_container_path = str(Path(_CONTAINER_WORKSPACE) / state_dir_relpath)
    else:
        provider_state_dir = role_session.path
        state_dir_container_path = str(
            Path(_CONTAINER_WORKSPACE)
            / role_session.path.relative_to(request.mount_path)
        )

    _auth_seed_action = service.auth_seed_action(provider_state_dir)
    if _auth_seed_action is not None:
        _auth_seed_action.apply()
    provider_auth = service.provider_auth()

    resolved_model = request.model or _default_model(service)
    resolved_effort = request.effort or _default_effort()

    git_name = git_service.get_user_name()
    git_email = git_service.get_user_email()

    session = build_session(request.mount_path, service, state_dir_container_path)
    runner = ContainerRunner(
        request.name,
        session,
        _ContainerRunnerConfig(
            cfg=cfg,
            model=resolved_model,
            effort=resolved_effort,
            status_display=status_display,
            service=service,
            mount_path=request.mount_path,
        ),
    )
    runtime_client = runner.get_runtime_client()

    model_display = ModelDisplayMetadata(
        service=service.name,
        model=resolved_model,
        effort=resolved_effort,
    )

    color_key: int | None = None
    if request.role in (AgentRole.IMPLEMENTER, AgentRole.REVIEWER):
        issue_number_str = request.prompt.scope_args.get("ISSUE_NUMBER", "")
        if issue_number_str.isdigit():
            color_key = int(issue_number_str)

    invocation = request.prompt
    role = request.role

    def _planned_protocol_reprompt(parser_error: str | None) -> str:
        def _render_expected_output_shape() -> str:
            return renderer.render_expected_output_shape(
                invocation.template,
                invocation.scope_args,
            )

        return protocol_reprompt.plan_protocol_reprompt(
            role=role,
            invocation=invocation,
            parser_error=parser_error if parser_error is not None else "unknown",
            render_expected_output_shape=_render_expected_output_shape,
        )

    async def _render_prompt_fn(req: RunRequest, run_kind: RunKind) -> str:
        loop = asyncio.get_running_loop()

        async def _container_exec(command: str) -> str:
            return await loop.run_in_executor(None, runner.exec_command, command)

        return await render_prompt_invocation(
            req.prompt,
            renderer=renderer,
            run_kind=run_kind,
            exec_fn=_container_exec,
        )

    return _WorkPreparation(
        role_session=role_session,
        provider_state_dir=provider_state_dir,
        provider_auth=provider_auth,
        resolved_model=resolved_model,
        resolved_effort=resolved_effort,
        runner=runner,
        runtime_client=runtime_client,
        model_display=model_display,
        color_key=color_key,
        planned_protocol_reprompt=_planned_protocol_reprompt,
        render_prompt=_render_prompt_fn,
        status_display=status_display,
        session=session,
        git_name=git_name,
        git_email=git_email,
        service=service,
    )
