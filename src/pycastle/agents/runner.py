import contextlib
import dataclasses
from collections.abc import Callable, Coroutine
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn, Protocol, Self, cast

import agent_runtime
import docker
import docker.errors
from agent_runtime.errors import (
    AgentCredentialFailureError,
    HardAgentError,
)
from agent_runtime.errors import (
    ContinuationUnrecoverableError as RuntimeContinuationUnrecoverableError,
)
from agent_runtime.runtime import (
    Completed,
    NewSessionRunRequest,
    ResumedSessionRunRequest,
)

from pycastle import _time as _time_module
from pycastle import stage_registry
from pycastle.agents._work_preparation import _CONTAINER_WORKSPACE, _prepare_work
from pycastle.agents.attempt_loop import (
    _decide_transition,
    _RaiseAgentFailed,
    _RaiseModelNotAvailable,
    _RaiseProviderUsageLimit,
    _RaiseTimeout,
    _RaiseTransientError,
    _RaiseUsageLimit,
    _Reprompt,
    _ResumeAfterTimeout,
    _ReturnCancelled,
    _ReturnParsed,
    _runtime_tool_policy_for_role,
    format_transient_status_message,
)
from pycastle.agents.output_protocol import (
    AgentOutput,
    AgentRole,
    AgentSuccessOutput,
    CompletionOutput,
    FailedOutput,
)
from pycastle.config import Config, image_name_for
from pycastle.display.rows import StatusRowConfig, status_row
from pycastle.display.status_display import (
    WORK_PHASE,
    ModelDisplayMetadata,
    PlainStatusDisplay,
    StatusDisplay,
)
from pycastle.errors import (
    AgentFailedError,
    AgentTimeoutError,
    DockerError,
    ModelNotAvailableError,
    SetupPhaseError,
    TransientAgentError,
    UsageLimitError,
)
from pycastle.execution_contracts import (
    CancellationToken,
    RuntimeInvocationDependencies,
    RuntimeModelDisplayMetadata,
    RuntimeStatusDisplay,
    RuntimeStatusRowConfig,
)
from pycastle.infrastructure.container_runner import (
    ContainerRunner,
    _ContainerRunnerConfig,
)
from pycastle.infrastructure.docker_session import DockerSession, build_volume_spec
from pycastle.infrastructure.preflight_failure_interpreter import (
    PreflightCommandFailure,
)
from pycastle.managed_worktree_mount_policy import enforce_managed_worktree_mount
from pycastle.prompts.dispatch import PromptInvocation
from pycastle.prompts.pipeline import PromptRenderer
from pycastle.prompts.scope_args import build_interrupted_work_clause
from pycastle.services import GitService
from pycastle.services._wake_time import (
    _minimum_unknown_reset_duration_for_provider,
    compute_wake_time,
)
from pycastle.services.runtime_services import (
    KNOWN_SERVICE_NAMES,
    AgentService,
    ClaudeService,
)
from pycastle.services.service_registry import ServiceRegistry
from pycastle.session import RoleSession, RunKind
from pycastle.session.service_session_store import ServiceSessionStore


def _minimum_unknown_reset_or_default(
    reset_time: datetime | None,
    minimum_unknown_reset_duration: timedelta,
    now: datetime,
) -> datetime | None:
    if reset_time is not None or minimum_unknown_reset_duration <= timedelta(0):
        return reset_time
    wake, _ = compute_wake_time(
        reset_time,
        now,
        minimum_unknown_reset_duration=minimum_unknown_reset_duration,
    )
    return wake - timedelta(minutes=2)


class _UnavailableDockerSession:
    def __init__(self, message: str) -> None:
        self._message = message

    def __enter__(self) -> Self:
        raise DockerError(self._message)

    def __exit__(self, *_args: object) -> None:
        return None

    def exec_simple(self, _command: str, timeout: float | None = None) -> str:
        del timeout
        raise DockerError(self._message)


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
    run_session_plan: Any = None
    preserve_session_on_completion: bool = False


async def translate_run_outcome(
    inner: Coroutine[Any, Any, AgentOutput], request: RunRequest
) -> AgentSuccessOutput:
    try:
        output = await inner
        if isinstance(output, FailedOutput):
            session_store = Path(".pycastle-session") / request.role.value
            if request.session_namespace:
                session_store = session_store / request.session_namespace
            if request.service:
                session_store = session_store / request.service
            raise AgentFailedError(
                role_value=request.role.value,
                worktree_path=request.mount_path,
                namespace=request.session_namespace,
                failure_class=output.failure_class,
                service_name=request.service or "claude",
                session_store=session_store,
            )
    except AgentTimeoutError as err:
        if not err.role_value:
            err.role_value = request.role.value
            err.worktree_path = request.mount_path
        raise
    else:
        return output


class AgentRunnerProtocol(Protocol):
    async def run(self, request: RunRequest) -> AgentSuccessOutput: ...

    async def run_preflight(
        self,
        *,
        name: str,
        mount_path: Path,
        status_display: StatusDisplay | None = None,
        work_body: str = "",
    ) -> list[PreflightCommandFailure]: ...


class AgentRunner:
    def __init__(
        self,
        env: dict[str, str],
        cfg: Config,
        git_service: GitService,
        docker_client: docker.DockerClient | None = None,
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

    def _runtime_service_registry(self) -> ServiceRegistry:
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
        try:
            return DockerSession(
                volumes=volumes,
                container_env=container_env,
                image_name=image_name_for(self._cfg.docker_image_name),
                cfg=self._cfg,
                docker_client=self._docker_client,
                auto_overlay=auto_overlay,
            )
        except docker.errors.DockerException as exc:
            return cast(
                "DockerSession",
                _UnavailableDockerSession(str(exc)),
            )

    def _handle_provider_account_exhaustion(
        self,
        service: AgentService,
        error: UsageLimitError,
    ) -> None:
        provider = error.provider or service.name
        minimum_unknown_reset_duration = _minimum_unknown_reset_duration_for_provider(
            self._cfg,
            provider,
        )
        mark_permanently_exhausted = getattr(
            service,
            "mark_permanently_exhausted",
            None,
        )
        if error.is_permanent and callable(mark_permanently_exhausted):
            error.account_label = mark_permanently_exhausted()
            return
        now = _time_module.now_local()
        mark_exhausted_reset_time = _minimum_unknown_reset_or_default(
            error.reset_time,
            minimum_unknown_reset_duration,
            now,
        )
        service.mark_exhausted(mark_exhausted_reset_time)

    def build_work_dependencies(
        self,
        *,
        name: str,
        model: str,
        effort: str,
        service: AgentService,
    ) -> RuntimeInvocationDependencies:
        def _status_row_factory(
            status_display: StatusDisplay,
            caller: str,
            *,
            kind: str,
            must_close: bool,
            config: RuntimeStatusRowConfig | None = None,
        ) -> AbstractAsyncContextManager[Any]:
            _cfg = config or RuntimeStatusRowConfig()
            model_display = _cfg.model_display
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
                kind=cast("Any", kind),
                must_close=must_close,
                config=StatusRowConfig(
                    color_key=_cfg.color_key,
                    work_body=_cfg.work_body,
                    initial_phase=_cfg.initial_phase,
                    startup_message=_cfg.startup_message,
                    model_display=pycastle_model_display,
                ),
            )

        def _prepare_session(_: object) -> NoReturn:
            raise RuntimeError(
                "prepare_session is always overridden in the one-shot path"
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
            error: UsageLimitError,
        ) -> None:
            self._handle_provider_account_exhaustion(service_for_run, error)

        return RuntimeInvocationDependencies(
            container_workspace=_CONTAINER_WORKSPACE,
            timeout_retries=self._cfg.timeout_retries,
            stage_key_for_role=stage_registry.stage_key_for_role,
            prepare_session=_prepare_session,
            build_session=cast(
                "Callable[[Path, AgentService, str | None], Any]",
                self._build_session,
            ),
            build_runner=lambda session, status_display, mount_path: ContainerRunner(
                name,
                session,
                _ContainerRunnerConfig(
                    cfg=self._cfg,
                    model=model,
                    effort=effort,
                    status_display=status_display,
                    service=service,
                    mount_path=mount_path,
                ),
            ),
            get_git_identity=lambda: (
                self._git_service.get_user_name(),
                self._git_service.get_user_email(),
            ),
            status_display_factory=lambda: cast(
                "RuntimeStatusDisplay", PlainStatusDisplay()
            ),
            status_row_factory=_status_row_factory,
            translate_setup_failure=_translate_setup_failure,
            build_model_display_metadata=lambda service_name, model_name, effort_name: (
                RuntimeModelDisplayMetadata(
                    service=service_name,
                    model=model_name,
                    effort=effort_name,
                )
            ),
            validate_mount_preconditions=lambda name, mount_path, role: (
                self._enforce_role_mount_precondition(
                    name=name,
                    mount_path=mount_path,
                    role=role,
                )
            ),
            handle_provider_account_exhaustion=cast(
                "Callable[[AgentService, Any], None]",
                _handle_provider_account_exhaustion,
            ),
            transient_status_message=format_transient_status_message,
        )

    def _build_preflight_session(self, mount_path: Path) -> DockerSession:
        volumes, auto_overlay = build_volume_spec(mount_path)
        return DockerSession(
            volumes=volumes,
            container_env=self._container_base_env(),
            image_name=image_name_for(self._cfg.docker_image_name),
            cfg=self._cfg,
            docker_client=self._docker_client,
            auto_overlay=auto_overlay,
        )

    def _enforce_role_mount_precondition(
        self,
        *,
        name: str,
        mount_path: Path,
        role: AgentRole,
    ) -> None:
        enforce_managed_worktree_mount(
            mount_path=mount_path,
            caller=name,
            role=role.value,
        )

    async def run(self, request: RunRequest) -> AgentSuccessOutput:
        self._enforce_role_mount_precondition(
            name=request.name,
            mount_path=request.mount_path,
            role=request.role,
        )
        return await translate_run_outcome(self._run(request), request)

    async def _run(self, request: RunRequest) -> AgentOutput:
        service = self._resolve_service(request.service)
        prep = _prepare_work(
            request,
            service,
            self._cfg,
            self._git_service,
            self._renderer,
            self._build_session,
        )

        async with status_row(
            prep.status_display,
            request.name,
            kind="agent",
            must_close=False,
            config=StatusRowConfig(
                color_key=prep.color_key,
                work_body=request.work_body,
                model_display=prep.model_display,
            ),
        ) as row:
            try:
                try:
                    await prep.runner.setup(prep.git_name, prep.git_email)
                except DockerError as exc:
                    raise SetupPhaseError(request.role.value, str(exc)) from exc
                prep.status_display.update_phase(request.name, WORK_PHASE)
                loop = _AttemptLoop(
                    host=self,
                    service=prep.service,
                    runner=prep.runner,
                    runtime_client=prep.runtime_client,
                    role_session=prep.role_session,
                    provider_state_dir=prep.provider_state_dir,
                    provider_auth=prep.provider_auth,
                    status_display=prep.status_display,
                    resolved_model=prep.resolved_model,
                    resolved_effort=prep.resolved_effort,
                    timeout_retries=self._cfg.timeout_retries,
                    idle_timeout=self._cfg.idle_timeout,
                    render_prompt=prep.render_prompt,
                    protocol_reprompt_plan=prep.planned_protocol_reprompt,
                )
                output = await loop.run(request)
                if request.token is not None and request.token.is_cancelled:
                    row.close("cancelled", shutdown_style="interrupted")
                else:
                    row.close("finished")
                return output
            finally:
                with contextlib.suppress(OSError):
                    prep.session.__exit__(None, None, None)

    async def run_preflight(
        self,
        *,
        name: str,
        mount_path: Path,
        status_display: StatusDisplay | None = None,
        work_body: str = "",
    ) -> list[PreflightCommandFailure]:
        if status_display is None:
            status_display = PlainStatusDisplay()

        git_name = self._git_service.get_user_name()
        git_email = self._git_service.get_user_email()
        async with status_row(
            status_display,
            name,
            kind="agent",
            must_close=False,
            config=StatusRowConfig(work_body=work_body, color_key=None),
        ) as row:
            session = self._build_preflight_session(mount_path)
            runner = ContainerRunner(
                name,
                session,
                _ContainerRunnerConfig(
                    cfg=self._cfg,
                    status_display=status_display,
                ),
            )
            try:
                try:
                    await runner.setup(git_name, git_email)
                except DockerError as exc:
                    raise SetupPhaseError("preflight", str(exc)) from exc
                failures = await runner.preflight(list(self._cfg.preflight_checks))
                if not failures:
                    row.close("finished, all tests green")
                return failures
            finally:
                with contextlib.suppress(OSError):
                    session.__exit__(None, None, None)


class _AttemptLoop:
    def __init__(  # noqa: PLR0913
        self,
        *,
        host: AgentRunner,
        service: AgentService,
        runner: ContainerRunner,
        runtime_client: agent_runtime.RuntimeClient,
        role_session: RoleSession,
        provider_state_dir: Path,
        provider_auth: agent_runtime.ProviderAuth | None,
        status_display: StatusDisplay,
        resolved_model: str,
        resolved_effort: str,
        timeout_retries: int,
        idle_timeout: int,
        render_prompt: Callable[..., Any],
        protocol_reprompt_plan: Callable[[str | None], str],
    ) -> None:
        self._host = host
        self.service = service
        self.runner = runner
        self.runtime_client = runtime_client
        self.role_session = role_session
        self.provider_state_dir = provider_state_dir
        self.provider_auth = provider_auth
        self.status_display = status_display
        self.resolved_model = resolved_model
        self.resolved_effort = resolved_effort
        self.timeout_retries = timeout_retries
        self.idle_timeout = idle_timeout
        self._render_prompt_fn = render_prompt
        self._protocol_reprompt_plan_fn = protocol_reprompt_plan

    async def _render_prompt(self, request: RunRequest, run_kind: RunKind) -> str:
        return await self._render_prompt_fn(request, run_kind)

    def _protocol_reprompt_plan(self, parser_error: str | None) -> str:
        return self._protocol_reprompt_plan_fn(parser_error)

    def _handle_provider_account_exhaustion(
        self, service: AgentService, error: UsageLimitError
    ) -> None:
        self._host._handle_provider_account_exhaustion(service, error)  # noqa: SLF001

    async def _recover_stale_continuation(
        self, request: RunRequest
    ) -> tuple[RunRequest, str]:
        self.role_session.start_fresh()
        is_dirty = not self._host._git_service.is_working_tree_clean(request.mount_path)  # noqa: SLF001
        if is_dirty:
            request = dataclasses.replace(
                request,
                prompt=PromptInvocation(
                    template=request.prompt.template,
                    scope_args={
                        **request.prompt.scope_args,
                        "INTERRUPTED_WORK": build_interrupted_work_clause(
                            RunKind.FRESH, is_dirty=True
                        ),
                    },
                    kind=request.prompt.kind,
                ),
            )
        new_prompt = await self._render_prompt(request, RunKind.FRESH)
        return request, new_prompt

    async def _run_runtime_once(
        self,
        request: RunRequest,
        prompt: str,
        run_kind: RunKind,
    ) -> Any:  # noqa: ANN401  # returns agent_runtime.RuntimeOutcome or compatible duck-typed object
        invocation_dir = request.mount_path
        logged_lines = [False]

        def _on_live_output(event: agent_runtime.AgentEvent) -> None:
            if self.runner.on_live_output(event):
                logged_lines[0] = True

        with self.runner.open_work_invocation(
            role=request.role,
            run_kind=run_kind,
            session_uuid=None,
            prompt=prompt,
        ):
            if run_kind is RunKind.RESUME and self.role_session.is_resumable():
                outcome = await self.runtime_client.run_resumed_session(
                    ResumedSessionRunRequest(
                        prompt=prompt,
                        invocation_dir=invocation_dir,
                        continuation=agent_runtime.Continuation(
                            serialized=self.role_session.read_continuation()
                        ),
                        provider_auth=self.provider_auth,
                        session_store=self.provider_state_dir,
                        timeout_seconds=self.idle_timeout,
                        on_live_output=_on_live_output,
                        token=cast("Any", request.token),
                        argv_transform=self.runner.provider_argv_transform(),
                    )
                )
            else:
                outcome = await self.runtime_client.run_new_session(
                    NewSessionRunRequest(
                        prompt=prompt,
                        invocation_dir=invocation_dir,
                        provider_selection=agent_runtime.ProviderSelection(
                            service=request.service,
                            model=self.resolved_model,
                            effort=self.resolved_effort,
                            auth=self.provider_auth,
                        ),
                        tool_policy=_runtime_tool_policy_for_role(request.role),
                        session_store=self.provider_state_dir,
                        timeout_seconds=self.idle_timeout,
                        name=request.name,
                        status_display=request.status_display,
                        work_body=request.work_body,
                        token=cast("Any", request.token),
                        on_live_output=_on_live_output,
                        argv_transform=self.runner.provider_argv_transform(),
                    )
                )
            if not logged_lines[0] and outcome.result.output:
                self.runner.append_chunk(outcome.result.output)
        return outcome

    async def run(self, request: RunRequest) -> AgentOutput:
        current_prompt = await self._render_prompt(
            request, self.role_session.run_kind()
        )
        current_run_kind = self.role_session.run_kind()
        retries_left = self.timeout_retries

        for attempt in range(3):
            _saved_service = (
                ServiceSessionStore(
                    self.role_session.path
                ).transcript_owner_service_name(KNOWN_SERVICE_NAMES)
                if current_run_kind is RunKind.RESUME
                and self.role_session.is_resumable()
                else None
            )
            if _saved_service is not None and _saved_service != request.service:
                request, current_prompt = await self._recover_stale_continuation(
                    request
                )
                current_run_kind = RunKind.FRESH

            try:
                outcome = await self._run_runtime_once(
                    request=request,
                    prompt=current_prompt,
                    run_kind=current_run_kind,
                )
            except AgentCredentialFailureError as err:
                err.caller = request.name
                raise
            except HardAgentError as err:
                err.caller = request.name
                raise
            except RuntimeContinuationUnrecoverableError:
                request, current_prompt = await self._recover_stale_continuation(
                    request
                )
                current_run_kind = RunKind.FRESH
                continue

            if not hasattr(outcome, "kind") and hasattr(outcome, "output"):
                outcome = agent_runtime.RuntimeOutcome(
                    kind=Completed(),
                    result=outcome,
                )

            continuation = outcome.result.continuation
            if continuation is not None and continuation.serialized is not None:
                self.role_session.write_continuation(continuation.serialized)

            directive = _decide_transition(
                outcome.kind,
                attempt=attempt,
                retries_left=retries_left,
                timeout_retries=self.timeout_retries,
                selected=outcome.result.selected,
                output_text=outcome.result.output or "",
                role=request.role,
                protocol_reprompt_plan=self._protocol_reprompt_plan,
                preserve_session_on_completion=request.preserve_session_on_completion,
                role_value=request.role.value,
                mount_path=request.mount_path,
                session_namespace=request.session_namespace,
                service_name=self.service.name,
                session_store=self.role_session.path,
                log_path=getattr(self.runner, "log_path", None),
                stage_key=stage_registry.stage_key_for_role(request.role),
            )

            match directive:
                case _ReturnCancelled():
                    return CompletionOutput()
                case _ReturnParsed(parsed=p, clear_completion=clear):
                    if clear:
                        self.role_session.clear_provider_state_and_signal_completion()
                    return p
                case _RaiseUsageLimit(reset_time=rt, provider=prov, is_permanent=perm):
                    error = UsageLimitError(
                        reset_time=rt, provider=prov, is_permanent=perm
                    )
                    self._handle_provider_account_exhaustion(self.service, error)
                    raise error
                case _RaiseTransientError(detail=detail):
                    transient_err = TransientAgentError(message=detail or "")
                    self.status_display.print(
                        request.name, format_transient_status_message(transient_err)
                    )
                    raise transient_err
                case _RaiseProviderUsageLimit(provider=prov, raw_message=msg):
                    error = UsageLimitError(provider=prov, raw_message=msg)
                    self._handle_provider_account_exhaustion(self.service, error)
                    raise error
                case _ResumeAfterTimeout(restart_num=n):
                    self.status_display.print(
                        request.name,
                        f"Timeout — restarting (attempt {n}/{self.timeout_retries})",
                    )
                    current_run_kind = RunKind.RESUME
                    current_prompt = await self._render_prompt(
                        request, current_run_kind
                    )
                    retries_left -= 1
                    continue
                case _RaiseTimeout(role_value=rv):
                    raise AgentTimeoutError("Provider timed out", role_value=rv)
                case _RaiseModelNotAvailable(service=svc, model=mdl, stage_key=sk):
                    self.service.mark_model_restricted(mdl)
                    raise ModelNotAvailableError(service=svc, model=mdl, stage_key=sk)
                case _Reprompt(message=msg):
                    current_prompt = msg
                    current_run_kind = RunKind.RESUME
                    continue
                case _RaiseAgentFailed() as d:
                    raise AgentFailedError(
                        role_value=d.role_value,
                        worktree_path=d.mount_path,
                        namespace=d.session_namespace,
                        failure_class="protocol_error",
                        service_name=d.service_name,
                        session_store=d.session_store,
                        agent_invocation_log_path=d.log_path,
                    )
        raise AssertionError("attempt loop exhausted without terminal directive")
