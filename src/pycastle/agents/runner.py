import asyncio
import dataclasses
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, Protocol

from .output_protocol import (
    AgentOutput,
    AgentOutputProtocolError,
    AgentRole,
    AgentSuccessOutput,
    FailedOutput,
)
from .result import CancellationToken
from ..config import Config, image_name_for
from ..infrastructure.container_runner import ContainerRunner
from ..infrastructure.docker_session import DockerSession, build_volume_spec
from ..errors import (
    AgentCredentialFailureError,
    AgentFailedError,
    AgentTimeoutError,
    DockerError,
    HardAgentError,
    SetupPhaseError,
    TransientAgentError,
    UsageLimitError,
)
from ..prompts.pipeline import PromptRenderer, PromptTemplate
from ..session import RunKind
from .session_dispatch import SessionDispatchRequest, prepare_agent_session
from ..session.agent import RunSessionPlan
from ..services import GitService
from ..services.agent_service import AgentService
from ..services.claude_service import ClaudeService
from ..display.status_display import ModelDisplayMetadata, PlainStatusDisplay
from ..infrastructure.preflight_failure_interpreter import PreflightCommandFailure

_CONTAINER_WORKSPACE = "/home/agent/workspace"

REPROMPT_MESSAGE = (
    "Your last response did not include the required protocol output. "
    "Please review the task requirements and try again, making sure to "
    "include the required output tag."
)


def _stage_key_for_role(role: AgentRole) -> str | None:
    mapping = {
        AgentRole.PLANNER: "plan",
        AgentRole.IMPLEMENTER: "implement",
        AgentRole.REVIEWER: "review",
        AgentRole.MERGER: "merge",
        AgentRole.PREFLIGHT_ISSUE: "preflight_issue",
        AgentRole.IMPROVE: "improve",
        AgentRole.FAILURE_REPORT: "preflight_issue",
    }
    return mapping.get(role)


@dataclasses.dataclass
class RunRequest:
    name: str
    template: PromptTemplate
    mount_path: Path
    role: AgentRole = AgentRole.IMPLEMENTER
    scope_args: dict[str, str] | None = None
    model: str = ""
    effort: str = ""
    service: str = ""
    stage: str = ""
    token: CancellationToken | None = None
    status_display: Any = None
    issue_title: str = ""
    work_body: str = ""
    send_role_prompt_on_resume: bool = False
    session_namespace: str = ""
    run_session_plan: RunSessionPlan | None = None


async def translate_run_outcome(
    inner: Coroutine[Any, Any, AgentOutput], request: RunRequest
) -> AgentSuccessOutput:
    try:
        output = await inner
        if isinstance(output, FailedOutput):
            raise AgentFailedError(
                role_value=request.role.value,
                worktree_path=request.mount_path,
                namespace=request.session_namespace,
                failure_class=output.failure_class,
                service_name=request.service,
            )
        return output
    except AgentTimeoutError as err:
        if not err.role_value:
            err.role_value = request.role.value
            err.worktree_path = request.mount_path
        raise


class AgentRunnerProtocol(Protocol):
    async def run(self, request: RunRequest) -> AgentSuccessOutput: ...

    async def run_preflight(
        self,
        *,
        name: str,
        mount_path: Path,
        stage: str = "",
        status_display=None,
        work_body: str = "",
    ) -> list[PreflightCommandFailure]: ...


class AgentRunner:
    def __init__(
        self,
        env: dict[str, str],
        cfg: Config,
        git_service: GitService,
        docker_client=None,
        service_registry: dict[str, AgentService] | None = None,
    ) -> None:
        self._env = env
        self._cfg = cfg
        self._git_service = git_service
        self._docker_client = docker_client
        self._service_registry = service_registry or {"claude": ClaudeService()}
        self._renderer = PromptRenderer(cfg)

    def _container_base_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        gh_token = self._env.get("GH_TOKEN")
        if gh_token:
            env["GH_TOKEN"] = gh_token
        return env

    def _resolve_service(self, service_name: str = "") -> AgentService:
        resolved_name = service_name.strip()
        if not resolved_name:
            raise ValueError("Agent dispatch requires an explicit resolved service")
        service = self._service_registry.get(resolved_name)
        if service is not None:
            return service
        raise ValueError(f"Unknown agent service {resolved_name!r}")

    def _build_session(
        self,
        mount_path: Path,
        service: AgentService,
        state_dir_container_path: str | None = None,
    ) -> DockerSession:
        volumes, auto_overlay = build_volume_spec(mount_path)
        container_env = self._container_base_env()
        container_env.update(service.build_env(state_dir_container_path))
        return DockerSession(
            volumes=volumes,
            container_env=container_env,
            image_name=image_name_for(self._cfg.docker_image_name, service.name),
            cfg=self._cfg,
            docker_client=self._docker_client,
            auto_overlay=auto_overlay,
        )

    def _build_preflight_session(self, mount_path: Path) -> DockerSession:
        volumes, auto_overlay = build_volume_spec(mount_path)
        return DockerSession(
            volumes=volumes,
            container_env=self._container_base_env(),
            image_name=image_name_for(self._cfg.docker_image_name, ""),
            cfg=self._cfg,
            docker_client=self._docker_client,
            auto_overlay=auto_overlay,
        )

    async def _build_prompt(
        self,
        template: PromptTemplate,
        scope_args: dict[str, str],
        container_exec: Callable[[str], Coroutine[Any, Any, str]],
        run_kind: RunKind,
        send_role_prompt_on_resume: bool,
    ) -> str:
        if run_kind == RunKind.RESUME and not send_role_prompt_on_resume:
            return await self._renderer.render(
                PromptTemplate.RESUME, {}, container_exec
            )
        return await self._renderer.render(template, scope_args, container_exec)

    async def run(self, request: RunRequest) -> AgentSuccessOutput:
        return await translate_run_outcome(self._run(request), request)

    async def _run(self, request: RunRequest) -> AgentOutput:
        from ..iteration._rows import status_row

        name = request.name
        template = request.template
        mount_path = request.mount_path
        role = request.role
        scope_args = request.scope_args or {}
        model = request.model
        effort = request.effort
        service = self._resolve_service(request.service)
        token = request.token
        status_display = request.status_display
        work_body = request.work_body

        if status_display is None:
            status_display = PlainStatusDisplay()

        _token = token if token is not None else CancellationToken()
        if _token.is_cancelled:
            raise UsageLimitError(reset_time=None, stage_key=_stage_key_for_role(role))

        prepared_session = prepare_agent_session(
            SessionDispatchRequest(
                mount_path=mount_path,
                role=role,
                session_namespace=request.session_namespace,
                service=service,
                container_workspace=_CONTAINER_WORKSPACE,
                run_session_plan=request.run_session_plan,
            )
        )
        non_typed_retry_done = False
        initial_attempt = True

        color_key: int | None = None
        if role in (AgentRole.IMPLEMENTER, AgentRole.REVIEWER):
            issue_number_str = scope_args.get("ISSUE_NUMBER", "")
            if issue_number_str.isdigit():
                color_key = int(issue_number_str)

        async with status_row(
            status_display,
            name,
            kind="agent",
            must_close=False,
            work_body=work_body,
            color_key=color_key,
            model_display=ModelDisplayMetadata(
                service=service.name,
                model=model,
                effort=effort,
            ),
        ) as row:
            session = self._build_session(
                mount_path,
                service,
                prepared_session.provider_state_dir_container_path,
            )
            runner = ContainerRunner(
                name,
                session,
                model=model,
                effort=effort,
                status_display=status_display,
                cfg=self._cfg,
                service=service,
            )
            try:
                git_name = self._git_service.get_user_name()
                git_email = self._git_service.get_user_email()
                try:
                    await runner.setup(git_name, git_email, work_body)
                except DockerError as exc:
                    raise SetupPhaseError(role.value, str(exc)) from exc

                prepared_session.prepare_for_run()

                loop = asyncio.get_running_loop()

                async def container_exec(cmd: str) -> str:
                    return await loop.run_in_executor(None, session.exec_simple, cmd)

                retries_left = self._cfg.timeout_retries
                while True:
                    try:
                        provider_run_session = (
                            prepared_session.initial_provider_run_session()
                            if initial_attempt
                            else prepared_session.resumable_provider_run_session()
                        )
                        prompt = await self._build_prompt(
                            template,
                            scope_args,
                            container_exec,
                            run_kind=provider_run_session.run_kind,
                            send_role_prompt_on_resume=request.send_role_prompt_on_resume,
                        )

                        work_prompt = prompt
                        work_run_session = provider_run_session
                        for _ in range(3):
                            try:
                                output = await runner.work(
                                    role,
                                    work_prompt,
                                    run_kind=work_run_session.run_kind,
                                    session_uuid=work_run_session.provider_session_id,
                                    on_provider_session_id=(
                                        work_run_session.record_provider_session_id
                                    ),
                                )
                                if not isinstance(output, FailedOutput):
                                    work_run_session.record_successful_run()
                                if isinstance(output, FailedOutput):
                                    row.close("failed", shutdown_style="error")
                                return output
                            except AgentOutputProtocolError:
                                next_run_session = prepared_session.protocol_reprompt_provider_run_session()
                                if next_run_session is None:
                                    row.close("failed", shutdown_style="error")
                                    return FailedOutput(failure_class="protocol_error")
                                work_prompt = REPROMPT_MESSAGE
                                work_run_session = next_run_session
                        row.close("failed", shutdown_style="error")
                        return FailedOutput(failure_class="protocol_error")
                    except AgentTimeoutError:
                        if retries_left <= 0:
                            raise
                        restart_num = self._cfg.timeout_retries - retries_left + 1
                        status_display.print(
                            name,
                            f"Timeout — restarting"
                            f" (attempt {restart_num}/{self._cfg.timeout_retries})",
                        )
                        retries_left -= 1
                        initial_attempt = False
                    except UsageLimitError as err:
                        if err.stage_key is None:
                            err.stage_key = _stage_key_for_role(role)
                        if err.is_permanent and isinstance(service, ClaudeService):
                            err.account_label = service.mark_permanently_exhausted()
                        else:
                            service.mark_exhausted(err.reset_time)
                        _token.cancel()
                        raise
                    except TransientAgentError as err:
                        _token.cancel()
                        status_code_str = (
                            str(err.status_code)
                            if err.status_code is not None
                            else "no status"
                        )
                        status_display.print(
                            name,
                            f"transient API error: status {status_code_str}",
                        )
                        raise
                    except AgentCredentialFailureError as err:
                        _token.cancel()
                        err.caller = name
                        err.service_name = service.name
                        raise
                    except HardAgentError as err:
                        _token.cancel()
                        err.caller = name
                        err.service_name = service.name
                        raise
                    except Exception:
                        if provider_run_session.run_kind != RunKind.RESUME:
                            raise
                        if non_typed_retry_done:
                            row.close("failed", shutdown_style="error")
                            return FailedOutput(failure_class="non_typed_crash")
                        non_typed_retry_done = True
            finally:
                try:
                    session.__exit__(None, None, None)
                except Exception:
                    pass

    async def run_preflight(
        self,
        *,
        name: str,
        mount_path: Path,
        stage: str = "",
        status_display=None,
        work_body: str = "",
    ) -> list[PreflightCommandFailure]:
        from ..iteration._rows import status_row

        if status_display is None:
            status_display = PlainStatusDisplay()

        git_name = self._git_service.get_user_name()
        git_email = self._git_service.get_user_email()
        async with status_row(
            status_display,
            name,
            kind="agent",
            must_close=False,
            work_body=work_body,
            color_key=None,
        ) as row:
            session = self._build_preflight_session(mount_path)
            runner = ContainerRunner(
                name,
                session,
                status_display=status_display,
                cfg=self._cfg,
            )
            try:
                try:
                    await runner.setup(git_name, git_email, work_body)
                except DockerError as exc:
                    raise SetupPhaseError("preflight", str(exc)) from exc
                failures = await runner.preflight(list(self._cfg.preflight_checks))
                if not failures:
                    row.close("finished, all tests green")
                return failures
            finally:
                try:
                    session.__exit__(None, None, None)
                except Exception:
                    pass
