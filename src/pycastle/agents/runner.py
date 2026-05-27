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
from ..config import Config
from ..infrastructure.container_runner import ContainerRunner
from ..infrastructure.docker_session import DockerSession, build_volume_spec
from ..errors import (
    AgentFailedError,
    AgentTimeoutError,
    HardAgentError,
    TransientAgentError,
    UsageLimitError,
)
from ..prompts.pipeline import PromptRenderer, PromptTemplate
from ..session import RoleSession, RunKind
from ..services import GitService
from ..services.agent_service import AgentService
from ..services.claude_service import ClaudeService
from ..display.status_display import PlainStatusDisplay

_CONTAINER_WORKSPACE = "/home/agent/workspace"

REPROMPT_MESSAGE = (
    "Your last response did not include the required protocol output. "
    "Please review the task requirements and try again, making sure to "
    "include the required output tag."
)


@dataclasses.dataclass
class RunRequest:
    name: str
    template: PromptTemplate
    mount_path: Path
    role: AgentRole = AgentRole.IMPLEMENTER
    scope_args: dict[str, str] | None = None
    model: str = ""
    effort: str = ""
    stage: str = ""
    token: CancellationToken | None = None
    status_display: Any = None
    issue_title: str = ""
    work_body: str = ""
    send_role_prompt_on_resume: bool = False
    session_namespace: str = ""


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
    ) -> list[tuple[str, str, str]]: ...


class AgentRunner:
    def __init__(
        self,
        env: dict[str, str],
        cfg: Config,
        git_service: GitService,
        docker_client=None,
        service: AgentService | None = None,
    ) -> None:
        self._env = env
        self._cfg = cfg
        self._git_service = git_service
        self._docker_client = docker_client
        self._service: AgentService = (
            service if service is not None else ClaudeService()
        )
        self._renderer = PromptRenderer(cfg)

    def _build_session(
        self,
        mount_path: Path,
        state_dir_relpath: str | None = None,
    ) -> DockerSession:
        volumes, auto_overlay = build_volume_spec(mount_path)
        container_env = dict(self._env)
        state_dir: str | None = None
        if state_dir_relpath is not None:
            state_dir = f"{_CONTAINER_WORKSPACE}/{state_dir_relpath}"
        container_env.update(self._service.build_env(state_dir))
        return DockerSession(
            volumes=volumes,
            container_env=container_env,
            image_name=self._cfg.docker_image_name,
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
        token = request.token
        status_display = request.status_display
        work_body = request.work_body

        if status_display is None:
            status_display = PlainStatusDisplay()

        _token = token if token is not None else CancellationToken()
        if _token.is_cancelled:
            raise UsageLimitError(reset_time=None)

        session_namespace = request.session_namespace
        role_session = RoleSession(mount_path, role, session_namespace)
        session_uuid = role_session.session_uuid()
        svc_state_relpath = self._service.state_dir_relpath(role, session_namespace)
        run_kind = (
            RunKind.RESUME
            if svc_state_relpath
            and self._service.is_resumable(mount_path / svc_state_relpath)
            else RunKind.FRESH
        )

        non_typed_retry_done = False

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
        ):
            session = self._build_session(mount_path, svc_state_relpath)
            runner = ContainerRunner(
                name,
                session,
                model=model,
                effort=effort,
                status_display=status_display,
                cfg=self._cfg,
                service=self._service,
            )
            try:
                git_name = self._git_service.get_user_name()
                git_email = self._git_service.get_user_email()
                await runner.setup(git_name, git_email, work_body)

                if run_kind == RunKind.FRESH:
                    role_session.start_fresh()

                if svc_state_relpath:
                    (mount_path / svc_state_relpath).mkdir(parents=True, exist_ok=True)

                loop = asyncio.get_running_loop()

                async def container_exec(cmd: str) -> str:
                    return await loop.run_in_executor(None, session.exec_simple, cmd)

                retries_left = self._cfg.timeout_retries
                while True:
                    try:
                        prompt = await self._build_prompt(
                            template,
                            scope_args,
                            container_exec,
                            run_kind=run_kind,
                            send_role_prompt_on_resume=request.send_role_prompt_on_resume,
                        )

                        work_prompt = prompt
                        work_run_kind = run_kind
                        for _ in range(3):
                            try:
                                return await runner.work(
                                    role,
                                    work_prompt,
                                    run_kind=work_run_kind,
                                    session_uuid=session_uuid,
                                )
                            except AgentOutputProtocolError:
                                work_prompt = REPROMPT_MESSAGE
                                work_run_kind = RunKind.RESUME
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
                    except UsageLimitError as err:
                        self._service.mark_exhausted(err.reset_time)
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
                    except HardAgentError as err:
                        _token.cancel()
                        err.caller = name
                        raise
                    except Exception:
                        if run_kind != RunKind.RESUME:
                            raise
                        if non_typed_retry_done:
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
    ) -> list[tuple[str, str, str]]:
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
            session = self._build_session(mount_path)
            runner = ContainerRunner(
                name,
                session,
                status_display=status_display,
                cfg=self._cfg,
                service=self._service,
            )
            try:
                await runner.setup(git_name, git_email, work_body)
                failures = await runner.preflight(list(self._cfg.preflight_checks))
                if not failures:
                    row.close("finished, all tests green")
                return failures
            finally:
                try:
                    session.__exit__(None, None, None)
                except Exception:
                    pass
