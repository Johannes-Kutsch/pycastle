import asyncio
import contextlib
import dataclasses
from collections.abc import Callable, Coroutine
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, Self, cast

import agent_runtime
import docker
import docker.errors
from agent_runtime import ProviderAuth
from agent_runtime.contracts import ToolPolicy as RuntimeToolPolicy
from agent_runtime.errors import (
    AgentCredentialFailureError,
    HardAgentError,
    ProviderUnavailableReason,
)
from agent_runtime.errors import (
    ContinuationUnrecoverableError as RuntimeContinuationUnrecoverableError,
)
from agent_runtime.runtime import (
    Cancelled,
    Completed,
    ModelNotAvailable,
    NewSessionRunRequest,
    ProviderUnavailable,
    ResumedSessionRunRequest,
    TimedOut,
    UsageLimited,
)

from pycastle import _time as _time_module
from pycastle.agents import protocol_reprompt
from pycastle.agents.output_protocol import (
    AgentOutput,
    AgentOutputProtocolError,
    AgentRole,
    AgentSuccessOutput,
    CompletionOutput,
    FailedOutput,
    extract_output,
)
from pycastle.agents.result import CancellationToken
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
    PromptRunRequest,
    RuntimeInvocationDependencies,
    RuntimeModelDisplayMetadata,
    RuntimeRunSession,
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
from pycastle.prompts.dispatch import PromptInvocation, render_prompt_invocation
from pycastle.prompts.pipeline import PromptRenderer
from pycastle.prompts.scope_args import build_interrupted_work_clause
from pycastle.runtime import run_prompt as run_runtime_prompt
from pycastle.runtime_session import ProviderSessionStateRequest
from pycastle.services import GitService
from pycastle.services._wake_time import compute_wake_time
from pycastle.services.runtime_services import AgentService, ClaudeService
from pycastle.services.service_registry import ServiceRegistry
from pycastle.session import RoleSession, RunKind
from pycastle.session.agent import (
    RunSessionPlan,
    run_session_plan_from_provider_run_state_plan,
)
from pycastle.session.run_dispatch import (
    AgentRunSessionState,
    RunSessionRequest,
    prepare_run_session,
)
from pycastle.session.service_session_store import ServiceSessionStore
from pycastle.session_planning import ProviderRunStatePlan

_CONTAINER_WORKSPACE = "/home/agent/workspace"
_MAX_PROTOCOL_RETRIES = 2


@dataclasses.dataclass
class _RetrySignal:
    """Signals that _invoke_runtime_attempts should continue with a new attempt."""

    prompt: str
    run_kind: RunKind
    retries_left: int | None


def format_transient_status_message(err: TransientAgentError) -> str:
    detail = str(err)
    return f"transient API error: {detail}" if detail else "transient API error"


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


def _minimum_unknown_reset_duration_for_provider(
    cfg: Config,
    provider: str,
) -> timedelta:
    if provider == "claude":
        return timedelta(hours=cfg.claude_minimum_unknown_reset_duration_hours)
    if provider == "codex":
        return timedelta(hours=cfg.codex_minimum_unknown_reset_duration_hours)
    if provider == "opencode":
        return timedelta(hours=cfg.opencode_minimum_unknown_reset_duration_hours)
    return timedelta(0)


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


def _provider_auth_from_env(env: dict[str, str]) -> ProviderAuth | None:
    claude_token = env.get("CLAUDE_CODE_OAUTH_TOKEN")
    opencode_api_key = env.get("OPENCODE_GO_API_KEY")
    if claude_token is None and opencode_api_key is None:
        return None
    return ProviderAuth(
        claude_code_oauth_token=claude_token,
        opencode_api_key=opencode_api_key,
    )


def _runtime_tool_policy_for_role(role: AgentRole) -> RuntimeToolPolicy:
    if role is AgentRole.PLANNER:
        return RuntimeToolPolicy.NO_FILE_MUTATION
    return RuntimeToolPolicy.UNRESTRICTED


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
    run_session_plan: RunSessionPlan | None = None
    preserve_session_on_completion: bool = False


@dataclasses.dataclass
class _RuntimeAttemptContext:
    request: RunRequest
    service: AgentService
    runner: ContainerRunner
    runtime_client: Any  # agent_runtime.RuntimeClient or _DockerlessRuntimeClient
    role_session: RoleSession
    provider_state_dir: Path
    provider_auth: ProviderAuth | None
    resolved_model: str
    resolved_effort: str
    status_display: StatusDisplay
    protocol_reprompt_plan: Callable[
        [str | None], protocol_reprompt.ProtocolRepromptPlan
    ]


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

        def _prepare_session(
            run_session_plan: RuntimeRunSession,
        ) -> AgentRunSessionState:
            plan_payload = run_session_plan.run_session_plan
            if isinstance(plan_payload, ProviderRunStatePlan):
                return prepare_run_session(
                    RunSessionRequest(
                        worktree=run_session_plan.mount_path,
                        role=cast("AgentRole", run_session_plan.role),
                        session_namespace=run_session_plan.session_namespace,
                        service=cast("AgentService", run_session_plan.service),
                        container_workspace=run_session_plan.container_workspace,
                        run_session_plan=run_session_plan_from_provider_run_state_plan(
                            role=cast("AgentRole", run_session_plan.role),
                            worktree=run_session_plan.mount_path,
                            namespace=run_session_plan.session_namespace,
                            service=cast("AgentService", run_session_plan.service),
                            provider_run_state_plan=plan_payload,
                        ),
                        require_exact_transcript_for_strict_resume=True,
                    )
                )
            return prepare_run_session(
                RunSessionRequest(
                    worktree=run_session_plan.mount_path,
                    role=cast("AgentRole", run_session_plan.role),
                    session_namespace=run_session_plan.session_namespace,
                    service=cast("AgentService", run_session_plan.service),
                    container_workspace=run_session_plan.container_workspace,
                    run_session_plan=cast(
                        "RunSessionPlan | None",
                        run_session_plan.run_session_plan,
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
            error: UsageLimitError,
        ) -> None:
            self._handle_provider_account_exhaustion(service_for_run, error)

        return RuntimeInvocationDependencies(
            container_workspace=_CONTAINER_WORKSPACE,
            timeout_retries=self._cfg.timeout_retries,
            stage_key_for_role=_stage_key_for_role,
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

    async def run_prompt(self, request: PromptRunRequest) -> str:
        self._enforce_role_mount_precondition(
            name=request.name,
            mount_path=request.worktree.host_path,
            role=AgentRole.IMPLEMENTER,
        )
        return await run_runtime_prompt(
            runner=cast("Any", self),
            service_registry=self._runtime_service_registry(),
            request=request,
        )

    async def _run(self, request: RunRequest) -> AgentOutput:
        invocation = request.prompt
        service = self._resolve_service(request.service)
        color_key: int | None = None
        if request.role in (AgentRole.IMPLEMENTER, AgentRole.REVIEWER):
            issue_number_str = invocation.scope_args.get("ISSUE_NUMBER", "")
            if issue_number_str.isdigit():
                color_key = int(issue_number_str)

        def _render_expected_output_shape() -> str:
            return self._renderer.render_expected_output_shape(
                invocation.template,
                invocation.scope_args,
            )

        def _planned_protocol_reprompt(
            parser_error: str | None,
        ) -> protocol_reprompt.ProtocolRepromptPlan:
            return protocol_reprompt.plan_protocol_reprompt(
                role=request.role,
                invocation=invocation,
                parser_error=parser_error if parser_error is not None else "unknown",
                render_expected_output_shape=_render_expected_output_shape,
            )

        return await self._run_with_runtime_client(
            request=request,
            service=service,
            protocol_reprompt_plan=_planned_protocol_reprompt,
            color_key=color_key,
        )

    async def _run_with_runtime_client(
        self,
        *,
        request: RunRequest,
        service: AgentService,
        protocol_reprompt_plan: Callable[
            [str | None], protocol_reprompt.ProtocolRepromptPlan
        ],
        color_key: int | None,
    ) -> AgentOutput:
        token = request.token if request.token is not None else CancellationToken()
        if token.is_cancelled or not service.is_available():
            raise UsageLimitError(
                reset_time=None,
                stage_key=_stage_key_for_role(request.role),
            )
        status_display = (
            request.status_display
            if request.status_display is not None
            else PlainStatusDisplay()
        )
        role_session = RoleSession(
            request.mount_path,
            request.role,
            request.session_namespace,
        )
        state_dir_relpath = service.state_dir_relpath(
            request.role, request.session_namespace
        )
        if state_dir_relpath is not None:
            provider_state_dir: Path = request.mount_path / state_dir_relpath
            state_dir_container_path = str(
                Path(_CONTAINER_WORKSPACE) / state_dir_relpath
            )
        else:
            provider_state_dir = role_session.path
            state_dir_container_path = str(
                Path(_CONTAINER_WORKSPACE)
                / role_session.path.relative_to(request.mount_path)
            )
        _seed_state = service.provider_session_state(
            ProviderSessionStateRequest(
                role_session=ServiceSessionStore(role_session.path, role_session),
                provider_state_dir=provider_state_dir,
                has_resumable_provider_state=role_session.is_resumable(),
            )
        )
        if _seed_state.auth_seed_action is not None:
            _seed_state.auth_seed_action.apply()
        provider_auth = _provider_auth_from_env(
            service.build_env(state_dir_container_path)
        )
        resolved_model = request.model or _default_model(service)
        resolved_effort = request.effort or _default_effort()
        git_name = self._git_service.get_user_name()
        git_email = self._git_service.get_user_email()
        session = self._build_session(
            request.mount_path,
            service,
            state_dir_container_path,
        )
        runner = ContainerRunner(
            request.name,
            session,
            _ContainerRunnerConfig(
                cfg=self._cfg,
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

        async with status_row(
            status_display,
            request.name,
            kind="agent",
            must_close=False,
            config=StatusRowConfig(
                color_key=color_key,
                work_body=request.work_body,
                model_display=model_display,
            ),
        ) as row:
            try:
                try:
                    await runner.setup(git_name, git_email)
                except DockerError as exc:
                    raise SetupPhaseError(request.role.value, str(exc)) from exc
                status_display.update_phase(request.name, WORK_PHASE)
                ctx = _RuntimeAttemptContext(
                    request=request,
                    service=service,
                    runner=runner,
                    runtime_client=runtime_client,
                    role_session=role_session,
                    provider_state_dir=provider_state_dir,
                    provider_auth=provider_auth,
                    resolved_model=resolved_model,
                    resolved_effort=resolved_effort,
                    status_display=status_display,
                    protocol_reprompt_plan=protocol_reprompt_plan,
                )
                output = await self._invoke_runtime_attempts(ctx=ctx)
                if token.is_cancelled:
                    row.close("cancelled", shutdown_style="interrupted")
                else:
                    row.close("finished")
                return output
            finally:
                with contextlib.suppress(OSError):
                    session.__exit__(None, None, None)

    async def _invoke_runtime_attempts(
        self,
        *,
        ctx: _RuntimeAttemptContext,
    ) -> AgentOutput:
        prompt = await self._render_runtime_prompt(
            request=ctx.request,
            runner=ctx.runner,
            run_kind=ctx.role_session.run_kind(),
        )
        current_prompt = prompt
        current_run_kind = ctx.role_session.run_kind()
        retries_left = self._cfg.timeout_retries

        for attempt in range(3):
            _saved_service = (
                ServiceSessionStore(
                    ctx.role_session.path
                ).exact_transcript_service_name()
                if current_run_kind is RunKind.RESUME
                and ctx.role_session.is_resumable()
                else None
            )
            if _saved_service is not None and _saved_service != ctx.request.service:
                ctx.request, current_prompt = await self._recover_stale_continuation(
                    role_session=ctx.role_session,
                    request=ctx.request,
                    runner=ctx.runner,
                )
                current_run_kind = RunKind.FRESH
            try:
                outcome = await self._run_runtime_once(
                    ctx=ctx,
                    prompt=current_prompt,
                    run_kind=current_run_kind,
                )
            except AgentCredentialFailureError as err:
                err.caller = ctx.request.name
                raise
            except HardAgentError as err:
                err.caller = ctx.request.name
                raise
            except RuntimeContinuationUnrecoverableError:
                ctx.request, current_prompt = await self._recover_stale_continuation(
                    role_session=ctx.role_session,
                    request=ctx.request,
                    runner=ctx.runner,
                )
                current_run_kind = RunKind.FRESH
                continue

            action = await self._handle_attempt_outcome(
                outcome,
                ctx=ctx,
                attempt=attempt,
                retries_left=retries_left,
            )
            if isinstance(action, _RetrySignal):
                current_prompt = action.prompt
                current_run_kind = action.run_kind
                if action.retries_left is not None:
                    retries_left = action.retries_left
                continue
            return action

        raise RuntimeError("Runtime reprompt loop exhausted unexpectedly")

    async def _handle_attempt_outcome(
        self,
        outcome: Any,  # noqa: ANN401  # agent_runtime.RuntimeOutcome or duck-typed
        *,
        ctx: _RuntimeAttemptContext,
        attempt: int,
        retries_left: int,
    ) -> AgentOutput | _RetrySignal:
        if not hasattr(outcome, "kind") and hasattr(outcome, "output"):
            outcome = agent_runtime.RuntimeOutcome(
                kind=Completed(),
                result=outcome,
            )
        continuation = outcome.result.continuation
        if continuation is not None and continuation.serialized is not None:
            ctx.role_session.write_continuation(continuation.serialized)
        if isinstance(outcome.kind, Cancelled):
            return CompletionOutput()
        if isinstance(outcome.kind, Completed):
            return self._handle_completed_outcome(
                outcome,
                ctx=ctx,
                attempt=attempt,
            )
        if isinstance(outcome.kind, UsageLimited):
            error = UsageLimitError(
                reset_time=outcome.kind.reset_time,
                provider=outcome.result.selected.service,
                is_permanent=outcome.kind.is_permanent,
            )
            self._handle_provider_account_exhaustion(ctx.service, error)
            raise error
        if isinstance(outcome.kind, ProviderUnavailable):
            if outcome.kind.reason is ProviderUnavailableReason.TRANSIENT_API_ERROR:
                ctx.status_display.print(
                    ctx.request.name,
                    format_transient_status_message(
                        TransientAgentError(message=outcome.kind.detail)
                    ),
                )
                raise TransientAgentError(message=outcome.kind.detail)
            error = UsageLimitError(
                provider=outcome.result.selected.service,
                raw_message=outcome.kind.detail,
            )
            self._handle_provider_account_exhaustion(ctx.service, error)
            raise error
        if isinstance(outcome.kind, TimedOut):
            return await self._handle_timed_out(
                request=ctx.request,
                runner=ctx.runner,
                status_display=ctx.status_display,
                retries_left=retries_left,
            )
        if isinstance(outcome.kind, ModelNotAvailable):
            model = outcome.result.selected.model
            ctx.service.mark_model_restricted(model)
            raise ModelNotAvailableError(
                service=outcome.result.selected.service,
                model=model,
                stage_key=_stage_key_for_role(ctx.request.role),
            )
        raise RuntimeError("Unexpected runtime outcome kind")

    def _handle_completed_outcome(
        self,
        outcome: Any,  # noqa: ANN401  # agent_runtime.RuntimeOutcome
        *,
        ctx: _RuntimeAttemptContext,
        attempt: int,
    ) -> AgentOutput | _RetrySignal:
        try:
            parsed = extract_output(outcome.result.output, ctx.request.role)
        except AgentOutputProtocolError as exc:
            if attempt == _MAX_PROTOCOL_RETRIES:
                raise AgentFailedError(
                    role_value=ctx.request.role.value,
                    worktree_path=ctx.request.mount_path,
                    namespace=ctx.request.session_namespace,
                    failure_class="protocol_error",
                    service_name=ctx.service.name,
                    session_store=ctx.role_session.path,
                    agent_invocation_log_path=getattr(ctx.runner, "log_path", None),
                ) from exc
            reprompt = ctx.protocol_reprompt_plan(str(exc))
            reprompt_message = (
                protocol_reprompt.GENERIC_PROTOCOL_REPROMPT_MESSAGE
                if isinstance(reprompt, protocol_reprompt.UnsupportedProtocolReprompt)
                else reprompt.message
            )
            return _RetrySignal(
                prompt=reprompt_message,
                run_kind=RunKind.RESUME,
                retries_left=None,
            )
        if not ctx.request.preserve_session_on_completion:
            ctx.role_session.clear_provider_state_and_signal_completion()
        return parsed

    async def _handle_timed_out(
        self,
        *,
        request: RunRequest,
        runner: ContainerRunner,
        status_display: StatusDisplay,
        retries_left: int,
    ) -> _RetrySignal:
        if retries_left <= 0:
            raise AgentTimeoutError(
                "Provider timed out",
                role_value=request.role.value,
            )
        restart_num = self._cfg.timeout_retries - retries_left + 1
        status_display.print(
            request.name,
            f"Timeout — restarting (attempt {restart_num}/{self._cfg.timeout_retries})",
        )
        new_run_kind = RunKind.RESUME
        new_prompt = await self._render_runtime_prompt(
            request=request,
            runner=runner,
            run_kind=new_run_kind,
        )
        return _RetrySignal(
            prompt=new_prompt,
            run_kind=new_run_kind,
            retries_left=retries_left - 1,
        )

    async def _recover_stale_continuation(
        self,
        *,
        role_session: RoleSession,
        request: RunRequest,
        runner: ContainerRunner,
    ) -> tuple[RunRequest, str]:
        role_session.start_fresh()
        is_dirty = not self._git_service.is_working_tree_clean(request.mount_path)
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
                    send_role_prompt_on_resume=request.prompt.send_role_prompt_on_resume,
                ),
            )
        new_prompt = await self._render_runtime_prompt(
            request=request, runner=runner, run_kind=RunKind.FRESH
        )
        return request, new_prompt

    async def _render_runtime_prompt(
        self,
        *,
        request: RunRequest,
        runner: ContainerRunner,
        run_kind: RunKind,
    ) -> str:
        loop = asyncio.get_running_loop()

        async def _container_exec(command: str) -> str:
            return await loop.run_in_executor(
                None,
                runner.exec_command,
                command,
            )

        return await render_prompt_invocation(
            request.prompt,
            renderer=self._renderer,
            run_kind=run_kind,
            exec_fn=_container_exec,
        )

    async def _run_runtime_once(
        self,
        *,
        ctx: _RuntimeAttemptContext,
        prompt: str,
        run_kind: RunKind,
    ) -> Any:  # noqa: ANN401  # returns agent_runtime.RuntimeOutcome or compatible duck-typed object
        invocation_dir = ctx.request.mount_path
        logged_lines = [False]

        def _on_live_output(event: agent_runtime.AgentEvent) -> None:
            if ctx.runner.on_live_output(event):
                logged_lines[0] = True

        with ctx.runner.open_work_invocation(
            role=ctx.request.role,
            run_kind=run_kind,
            session_uuid=None,
            prompt=prompt,
        ):
            if run_kind is RunKind.RESUME and ctx.role_session.is_resumable():
                outcome = await ctx.runtime_client.run_resumed_session(
                    ResumedSessionRunRequest(
                        prompt=prompt,
                        invocation_dir=invocation_dir,
                        continuation=agent_runtime.Continuation(
                            serialized=ctx.role_session.read_continuation()
                        ),
                        provider_auth=ctx.provider_auth,
                        session_store=ctx.provider_state_dir,
                        timeout_seconds=self._cfg.idle_timeout,
                        on_live_output=_on_live_output,
                        token=cast("Any", ctx.request.token),
                        argv_transform=ctx.runner.provider_argv_transform(),
                    )
                )
            else:
                outcome = await ctx.runtime_client.run_new_session(
                    NewSessionRunRequest(
                        prompt=prompt,
                        invocation_dir=invocation_dir,
                        provider_selection=agent_runtime.ProviderSelection(
                            service=ctx.request.service,
                            model=ctx.resolved_model,
                            effort=ctx.resolved_effort,
                            auth=ctx.provider_auth,
                        ),
                        tool_policy=_runtime_tool_policy_for_role(ctx.request.role),
                        session_store=ctx.provider_state_dir,
                        timeout_seconds=self._cfg.idle_timeout,
                        name=ctx.request.name,
                        status_display=ctx.request.status_display,
                        work_body=ctx.request.work_body,
                        token=cast("Any", ctx.request.token),
                        on_live_output=_on_live_output,
                        argv_transform=ctx.runner.provider_argv_transform(),
                    )
                )
            if not logged_lines[0] and outcome.result.output:
                ctx.runner.append_chunk(outcome.result.output)
        return outcome

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
