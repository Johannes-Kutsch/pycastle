import dataclasses
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from pycastle_agent_runtime.work import (
    WorkModelDisplayMetadata,
    WorkInvocationDependencies,
    WorkInvocationRequest,
)

from ._work_invocation import ProtocolOutputAdapter, format_transient_status_message
from .output_protocol import (
    AgentOutput,
    AgentRole,
    AgentSuccessOutput,
    FailedOutput,
)
from .result import CancellationToken
from ..config import Config, StageOverride, image_name_for
from ..infrastructure.container_runner import ContainerRunner
from ..infrastructure.docker_session import DockerSession, build_volume_spec
from ..errors import (
    AgentFailedError,
    AgentTimeoutError,
    DockerError,
    SetupPhaseError,
)
from ..prompts.dispatch import (
    PromptInvocation,
    render_prompt_invocation,
)
from ..prompts.pipeline import PromptRenderer
from ..session import RunKind
from ..session.agent import RunSessionPlan
from ..session.resume import provider_state_relpath
from ..session.run_dispatch import RunSessionRequest, prepare_run_session
from ..services import GitService
from ..services.agent_service import AgentService
from ..services.claude_service import ClaudeService
from ..services.flag_profiles import AgentToolPolicyGroup
from ..display.status_display import (
    ModelDisplayMetadata,
    PlainStatusDisplay,
    StatusDisplay,
)
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
    prompt: PromptInvocation
    mount_path: Path
    role: AgentRole = AgentRole.IMPLEMENTER
    model: str = ""
    effort: str = ""
    service: str = ""
    stage: str = ""
    token: CancellationToken | None = None
    status_display: Any = None
    issue_title: str = ""
    work_body: str = ""
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
                provider_session_path=provider_state_relpath(
                    request.role,
                    request.service,
                    request.session_namespace,
                ).rstrip("/"),
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

    def resolve_service(self, service_name: str = "") -> AgentService:
        return self._resolve_service(service_name)

    def _runtime_service_registry(self):
        from pycastle_agent_runtime.service_registry import ServiceRegistry

        return ServiceRegistry(self._service_registry)

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

    def build_work_dependencies(
        self,
        *,
        name: str,
        model: str,
        effort: str,
        service: AgentService,
    ) -> WorkInvocationDependencies:
        def _status_row_factory(
            status_display: StatusDisplay,
            caller: str,
            *,
            kind: Literal["phase", "agent"],
            must_close: bool,
            color_key: int | None = None,
            work_body: str = "",
            initial_phase: str = "Setup",
            startup_message: str = "started",
            model_display: ModelDisplayMetadata | None = None,
        ) -> AbstractAsyncContextManager[Any]:
            from ..iteration._rows import status_row

            pycastle_model_display = (
                None
                if model_display is None
                else ModelDisplayMetadata(
                    service=model_display.service,
                    model=model_display.model,
                    effort=model_display.effort,
                )
            )
            return status_row(
                status_display,
                caller,
                kind=kind,
                must_close=must_close,
                color_key=color_key,
                work_body=work_body,
                initial_phase=initial_phase,
                startup_message=startup_message,
                model_display=pycastle_model_display,
            )

        def _prepare_session(**kwargs: object):
            return prepare_run_session(
                RunSessionRequest(
                    worktree=cast(Path, kwargs["mount_path"]),
                    role=cast(AgentRole, kwargs["role"]),
                    session_namespace=cast(str, kwargs["session_namespace"]),
                    service=cast(AgentService, kwargs["service"]),
                    container_workspace=cast(str, kwargs["container_workspace"]),
                    run_session_plan=cast(
                        RunSessionPlan | None,
                        kwargs["run_session_plan"],
                    ),
                )
            )

        def _translate_setup_failure(
            role: AgentRole,
            exc: BaseException,
        ) -> BaseException | None:
            if not isinstance(exc, DockerError):
                return None
            return SetupPhaseError(role.value, str(exc))

        def _handle_provider_account_exhaustion(
            service_for_run: AgentService,
            error,
        ) -> None:
            mark_permanently_exhausted = getattr(
                service_for_run,
                "mark_permanently_exhausted",
                None,
            )
            if error.is_permanent and callable(mark_permanently_exhausted):
                error.account_label = mark_permanently_exhausted()
                return
            service_for_run.mark_exhausted(error.reset_time)

        return WorkInvocationDependencies(
            container_workspace=_CONTAINER_WORKSPACE,
            timeout_retries=self._cfg.timeout_retries,
            stage_key_for_role=_stage_key_for_role,
            prepare_session=_prepare_session,
            build_session=self._build_session,
            build_runner=lambda session, status_display: ContainerRunner(
                name,
                session,
                model=model,
                effort=effort,
                status_display=status_display,
                cfg=self._cfg,
                service=service,
            ),
            get_git_identity=lambda: (
                self._git_service.get_user_name(),
                self._git_service.get_user_email(),
            ),
            status_row_factory=_status_row_factory,
            translate_setup_failure=_translate_setup_failure,
            build_model_display_metadata=lambda service_name, model_name, effort_name: (
                WorkModelDisplayMetadata(
                    service=service_name,
                    model=model_name,
                    effort=effort_name,
                )
            ),
            handle_provider_account_exhaustion=_handle_provider_account_exhaustion,
            transient_status_message=format_transient_status_message,
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

    async def run(self, request: RunRequest) -> AgentSuccessOutput:
        return await translate_run_outcome(self._run(request), request)

    async def run_prompt(
        self,
        *,
        name: str,
        prompt: str,
        mount_path: Path,
        model: str,
        effort: str,
        service: str,
        tool_policy: AgentToolPolicyGroup = AgentToolPolicyGroup.FULL,
        token: CancellationToken | None = None,
        status_display: Any = None,
        work_body: str = "",
        session_namespace: str = "",
        run_session_plan: RunSessionPlan | None = None,
    ) -> str:
        from pycastle_agent_runtime.runtime import (
            PromptRunRequest,
            PromptRunSession,
            ToolPolicy as RuntimeToolPolicy,
            WorktreeMount,
            run_prompt as run_runtime_prompt,
        )

        return await run_runtime_prompt(
            runner=self,
            service_registry=self._runtime_service_registry(),
            request=PromptRunRequest(
                name=name,
                prompt=prompt,
                worktree=WorktreeMount(mount_path),
                override=StageOverride(service=service, model=model, effort=effort),
                tool_policy=RuntimeToolPolicy(tool_policy.value),
                status_display=status_display,
                work_body=work_body,
                token=token,
                session=PromptRunSession(
                    namespace=session_namespace,
                    plan=run_session_plan,
                ),
            ),
        )

    async def _run(self, request: RunRequest) -> AgentOutput:
        invocation = request.prompt
        service = self._resolve_service(request.service)
        color_key: int | None = None
        if request.role in (AgentRole.IMPLEMENTER, AgentRole.REVIEWER):
            issue_number_str = invocation.scope_args.get("ISSUE_NUMBER", "")
            if issue_number_str.isdigit():
                color_key = int(issue_number_str)

        dependencies = self.build_work_dependencies(
            name=request.name,
            model=request.model,
            effort=request.effort,
            service=service,
        )

        async def prompt_factory(
            *,
            run_kind: RunKind,
            container_exec: Callable[[str], Awaitable[str]],
        ) -> str:
            return await render_prompt_invocation(
                invocation,
                renderer=self._renderer,
                run_kind=run_kind,
                exec_fn=container_exec,
            )

        from pycastle_agent_runtime.work import invoke_work

        return await invoke_work(
            WorkInvocationRequest(
                name=request.name,
                mount_path=request.mount_path,
                role=request.role,
                service=service,
                model=request.model,
                effort=request.effort,
                output_adapter=ProtocolOutputAdapter(
                    prompt_factory=prompt_factory,
                    reprompt_message=REPROMPT_MESSAGE,
                ),
                dependencies=dependencies,
                status_display=request.status_display,
                token=request.token,
                work_body=request.work_body,
                session_namespace=request.session_namespace,
                run_session_plan=request.run_session_plan,
                color_key=color_key,
                allow_non_typed_resume_retry=True,
            )
        )

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
