import asyncio
import dataclasses
from collections.abc import Callable, Coroutine
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import agent_runtime
from agent_runtime import ProviderAuth
from agent_runtime.contracts import ToolPolicy as RuntimeToolPolicy
from agent_runtime.errors import (
    AgentCredentialFailureError as RuntimeAgentCredentialFailureError,
    ContinuationUnrecoverableError as RuntimeContinuationUnrecoverableError,
)
from agent_runtime.errors import HardAgentError as RuntimeHardAgentError
from agent_runtime.errors import ProviderUnavailableReason
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

from .. import _time as _time_module
from ..execution_contracts import (
    RuntimeInvocationDependencies,
    RuntimeModelDisplayMetadata,
    RuntimeRunSession,
)
from pycastle.services._wake_time import compute_wake_time
from .output_protocol import (
    AgentOutput,
    AgentOutputProtocolError,
    CompletionOutput,
    AgentRole,
    AgentSuccessOutput,
    FailedOutput,
    extract_output,
)
from .result import CancellationToken
from ..config import Config, StageOverride, image_name_for
from ..infrastructure.container_runner import ContainerRunner
from ..infrastructure.docker_session import DockerSession, build_volume_spec
from ..errors import (
    AgentCredentialFailureError,
    AgentFailedError,
    AgentTimeoutError,
    DockerError,
    HardAgentError,
    ModelNotAvailableError,
    SetupPhaseError,
    TransientAgentError,
    UsageLimitError,
)
from ..managed_worktree_mount_policy import enforce_managed_worktree_mount
from ..prompts.dispatch import (
    PromptInvocation,
    render_prompt_invocation,
)
from ..prompts.scope_args import build_interrupted_work_clause
from ..prompts.pipeline import PromptRenderer
from ..runtime_session import ProviderSessionStateRequest
from ..session import RoleSession, RunKind
from ..session.agent import (
    RunSessionPlan,
    run_session_plan_from_provider_run_state_plan,
)
from ..session.run_dispatch import RunSessionRequest, prepare_run_session
from ..session_planning import ProviderRunStatePlan
from ..services import GitService
from ..services.runtime_services import (
    AgentService,
    ClaudeService,
    ToolPolicy as ServiceToolPolicy,
)
from . import protocol_reprompt
from ..display.status_display import (
    ModelDisplayMetadata,
    PlainStatusDisplay,
    StatusDisplay,
    WORK_PHASE,
)
from ..infrastructure.preflight_failure_interpreter import PreflightCommandFailure

_CONTAINER_WORKSPACE = "/home/agent/workspace"


def format_transient_status_message(err: TransientAgentError) -> str:
    return (
        "transient API error: status "
        f"{err.status_code if err.status_code is not None else 'no status'}"
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
    if role in {AgentRole.PLANNER, AgentRole.DIVERGENCE_RESOLVER}:
        return RuntimeToolPolicy.NO_FILE_MUTATION
    return RuntimeToolPolicy.UNRESTRICTED


def _default_effort() -> str:
    return "medium"


def _default_model(service: AgentService) -> str:
    try:
        valid_models = service.valid_models()
    except Exception:
        return "gpt-5.5"
    for candidate in ("gpt-5.5", "gpt-5.4", "haiku", "opus", "sonnet"):
        if candidate in valid_models:
            return candidate
    if valid_models:
        return sorted(valid_models)[0]
    return "gpt-5.5"


class _UnavailableDockerSession:
    def __init__(self, message: str) -> None:
        self._message = message

    def __enter__(self) -> "_UnavailableDockerSession":
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
        from pycastle.services.service_registry import ServiceRegistry

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
                image_name=image_name_for(self._cfg.docker_image_name, service.name),
                cfg=self._cfg,
                docker_client=self._docker_client,
                auto_overlay=auto_overlay,
            )
        except Exception as exc:
            return cast(
                DockerSession,
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

        def _prepare_session(
            run_session_plan: RuntimeRunSession,
        ):
            plan_payload = run_session_plan.run_session_plan
            if isinstance(plan_payload, ProviderRunStatePlan):
                return prepare_run_session(
                    RunSessionRequest(
                        worktree=run_session_plan.mount_path,
                        role=cast(AgentRole, run_session_plan.role),
                        session_namespace=run_session_plan.session_namespace,
                        service=cast(AgentService, run_session_plan.service),
                        container_workspace=run_session_plan.container_workspace,
                        run_session_plan=run_session_plan_from_provider_run_state_plan(
                            role=cast(AgentRole, run_session_plan.role),
                            worktree=run_session_plan.mount_path,
                            namespace=run_session_plan.session_namespace,
                            service=cast(AgentService, run_session_plan.service),
                            provider_run_state_plan=plan_payload,
                        ),
                        require_exact_transcript_for_strict_resume=True,
                    )
                )
            return prepare_run_session(
                RunSessionRequest(
                    worktree=run_session_plan.mount_path,
                    role=cast(AgentRole, run_session_plan.role),
                    session_namespace=run_session_plan.session_namespace,
                    service=cast(AgentService, run_session_plan.service),
                    container_workspace=run_session_plan.container_workspace,
                    run_session_plan=cast(
                        RunSessionPlan | None,
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
            error,
        ) -> None:
            self._handle_provider_account_exhaustion(service_for_run, error)

        return RuntimeInvocationDependencies(
            container_workspace=_CONTAINER_WORKSPACE,
            timeout_retries=self._cfg.timeout_retries,
            stage_key_for_role=_stage_key_for_role,
            prepare_session=_prepare_session,
            build_session=cast(
                Callable[[Path, AgentService, str | None], Any],
                self._build_session,
            ),
            build_runner=lambda session, status_display, mount_path: ContainerRunner(
                name,
                session,
                model=model,
                effort=effort,
                status_display=status_display,
                cfg=self._cfg,
                service=service,
                mount_path=mount_path,
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
                Callable[[AgentService, Any], None],
                _handle_provider_account_exhaustion,
            ),
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

    async def run_prompt(
        self,
        *,
        name: str,
        prompt: str,
        mount_path: Path,
        model: str,
        effort: str,
        service: str,
        tool_policy: ServiceToolPolicy = ServiceToolPolicy.FULL,
        token: CancellationToken | None = None,
        status_display: Any = None,
        work_body: str = "",
        session_namespace: str = "",
        run_session_plan: RunSessionPlan | None = None,
    ) -> str:
        from pycastle.runtime import (
            PromptRunRequest,
            PromptRunSession,
            ToolPolicy as RuntimeToolPolicy,
            WorktreeMount,
            run_prompt as run_runtime_prompt,
        )

        self._enforce_role_mount_precondition(
            name=name,
            mount_path=mount_path,
            role=AgentRole.IMPLEMENTER,
        )
        return await run_runtime_prompt(
            runner=cast(Any, self),
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
        from ..iteration._rows import status_row

        token = request.token if request.token is not None else CancellationToken()
        if token.is_cancelled:
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
                role_session=role_session,
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
            model=resolved_model,
            effort=resolved_effort,
            status_display=status_display,
            cfg=self._cfg,
            service=service,
            mount_path=request.mount_path,
        )
        runtime_client = runner._get_runtime_client()
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
            color_key=color_key,
            work_body=request.work_body,
            model_display=model_display,
        ) as row:
            try:
                try:
                    await runner.setup(git_name, git_email, request.work_body)
                except DockerError as exc:
                    raise SetupPhaseError(request.role.value, str(exc)) from exc
                status_display.update_phase(request.name, WORK_PHASE)
                output = await self._invoke_runtime_attempts(
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
                row.close("finished")
                return output
            finally:
                try:
                    session.__exit__(None, None, None)
                except Exception:
                    pass

    async def _invoke_runtime_attempts(
        self,
        *,
        request: RunRequest,
        service: AgentService,
        runner: ContainerRunner,
        runtime_client: Any,
        role_session: RoleSession,
        provider_state_dir: Path,
        provider_auth: ProviderAuth | None,
        resolved_model: str,
        resolved_effort: str,
        status_display: StatusDisplay,
        protocol_reprompt_plan: Callable[
            [str | None], protocol_reprompt.ProtocolRepromptPlan
        ],
    ) -> AgentOutput:
        prompt = await self._render_runtime_prompt(
            request=request,
            runner=runner,
            run_kind=role_session.run_kind(),
        )
        current_prompt = prompt
        current_run_kind = role_session.run_kind()
        retries_left = self._cfg.timeout_retries

        for attempt in range(3):
            _saved_service = (
                role_session.exact_transcript_service_name()
                if current_run_kind is RunKind.RESUME and role_session.is_resumable()
                else None
            )
            if _saved_service is not None and _saved_service != request.service:
                request, current_prompt = await self._recover_stale_continuation(
                    role_session=role_session, request=request, runner=runner
                )
                current_run_kind = RunKind.FRESH
            try:
                outcome = await self._run_runtime_once(
                    request=request,
                    runner=runner,
                    runtime_client=runtime_client,
                    role_session=role_session,
                    provider_state_dir=provider_state_dir,
                    provider_auth=provider_auth,
                    prompt=current_prompt,
                    run_kind=current_run_kind,
                    resolved_model=resolved_model,
                    resolved_effort=resolved_effort,
                )
            except RuntimeAgentCredentialFailureError as err:
                if request.token is not None:
                    request.token.cancel()
                mapped = AgentCredentialFailureError(
                    str(err),
                    service_name=err.service_name or service.name,
                    classification=err.classification,
                )
                mapped.caller = request.name
                if mapped.service_name == "opencode":
                    transformed = UsageLimitError(
                        reset_time=None,
                        raw_message=str(mapped),
                        provider=service.name,
                        is_permanent=True,
                    )
                    self._handle_provider_account_exhaustion(service, transformed)
                    if service.is_available():
                        provider_auth = _provider_auth_from_env(
                            service.build_env(str(role_session.path))
                        )
                        current_run_kind = RunKind.FRESH
                        current_prompt = await self._render_runtime_prompt(
                            request=request,
                            runner=runner,
                            run_kind=current_run_kind,
                        )
                        continue
                raise mapped from err
            except RuntimeHardAgentError as err:
                if request.token is not None:
                    request.token.cancel()
                mapped_hard = HardAgentError(
                    str(err),
                    service_name=err.service_name or service.name,
                    classification=err.classification,
                )
                mapped_hard.caller = request.name
                raise mapped_hard from err
            except RuntimeContinuationUnrecoverableError:
                request, current_prompt = await self._recover_stale_continuation(
                    role_session=role_session, request=request, runner=runner
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
                role_session.write_continuation(continuation.serialized)
            if isinstance(outcome.kind, Cancelled):
                return CompletionOutput()
            if isinstance(outcome.kind, Completed):
                try:
                    parsed = extract_output(outcome.result.output, request.role)
                except AgentOutputProtocolError as exc:
                    if attempt == 2:
                        raise AgentFailedError(
                            role_value=request.role.value,
                            worktree_path=request.mount_path,
                            namespace=request.session_namespace,
                            failure_class="protocol_error",
                            service_name=service.name,
                            session_store=role_session.path,
                            agent_invocation_log_path=getattr(runner, "log_path", None),
                        ) from exc
                    reprompt = protocol_reprompt_plan(str(exc))
                    current_prompt = (
                        protocol_reprompt.GENERIC_PROTOCOL_REPROMPT_MESSAGE
                        if isinstance(
                            reprompt,
                            protocol_reprompt.UnsupportedProtocolReprompt,
                        )
                        else reprompt.message
                    )
                    current_run_kind = RunKind.RESUME
                    continue
                role_session.clear_provider_state_and_signal_completion()
                return parsed
            if isinstance(outcome.kind, UsageLimited):
                error = UsageLimitError(
                    reset_time=outcome.kind.reset_time,
                    provider=outcome.result.selected.service,
                )
                self._handle_provider_account_exhaustion(service, error)
                if request.token is not None:
                    request.token.cancel()
                raise error
            if isinstance(outcome.kind, ProviderUnavailable):
                if outcome.kind.reason is ProviderUnavailableReason.TRANSIENT_API_ERROR:
                    if request.token is not None:
                        request.token.cancel()
                    status_display.print(
                        request.name,
                        format_transient_status_message(
                            TransientAgentError(message=outcome.kind.detail)
                        ),
                    )
                    raise TransientAgentError(message=outcome.kind.detail)
                error = UsageLimitError(
                    provider=outcome.result.selected.service,
                    raw_message=outcome.kind.detail,
                )
                self._handle_provider_account_exhaustion(service, error)
                if request.token is not None:
                    request.token.cancel()
                raise error
            if isinstance(outcome.kind, TimedOut):
                if outcome.result.selected.service == "opencode":
                    error = UsageLimitError(
                        provider=outcome.result.selected.service,
                    )
                    self._handle_provider_account_exhaustion(service, error)
                    if request.token is not None:
                        request.token.cancel()
                    raise error
                if retries_left <= 0:
                    raise AgentTimeoutError(
                        "Provider timed out",
                        role_value=request.role.value,
                    )
                restart_num = self._cfg.timeout_retries - retries_left + 1
                status_display.print(
                    request.name,
                    "Timeout — restarting"
                    f" (attempt {restart_num}/{self._cfg.timeout_retries})",
                )
                retries_left -= 1
                current_run_kind = RunKind.RESUME
                current_prompt = await self._render_runtime_prompt(
                    request=request,
                    runner=runner,
                    run_kind=current_run_kind,
                )
                continue
            if isinstance(outcome.kind, ModelNotAvailable):
                model = outcome.result.selected.model
                service.mark_model_restricted(model)
                if request.token is not None:
                    request.token.cancel()
                raise ModelNotAvailableError(
                    service=outcome.result.selected.service,
                    model=model,
                    stage_key=_stage_key_for_role(request.role),
                )
            raise RuntimeError("Unexpected runtime outcome kind")

        raise RuntimeError("Runtime reprompt loop exhausted unexpectedly")

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
                runner._session.exec_simple,
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
        request: RunRequest,
        runner: ContainerRunner,
        runtime_client: Any,
        role_session: RoleSession,
        provider_state_dir: Path,
        provider_auth: ProviderAuth | None,
        prompt: str,
        run_kind: RunKind,
        resolved_model: str,
        resolved_effort: str,
    ) -> Any:
        invocation_dir = request.mount_path
        logged_lines = [False]

        def _on_live_output(event: Any) -> None:
            runner._status_display.reset_idle_timer(runner.name)
            raw_provider_output = getattr(event, "raw_provider_output", "")
            if raw_provider_output and runner._current_work_invocation is not None:
                chunk = raw_provider_output
                if not chunk.endswith("\n"):
                    chunk += "\n"
                runner._current_work_invocation.append_provider_chunk(chunk.encode())
                logged_lines[0] = True
            if getattr(event, "type", None) == "agent_message":
                display_message = getattr(event, "display_message", "")
                if display_message:
                    runner._status_display.print(runner.name, display_message)

        with runner._logical_session.open_work_invocation(
            role=request.role,
            run_kind=run_kind,
            session_uuid=None,
            prompt=prompt,
        ) as work_invocation:
            runner._current_work_invocation = work_invocation
            try:
                if run_kind is RunKind.RESUME and role_session.is_resumable():
                    outcome = await runtime_client.run_resumed_session(
                        ResumedSessionRunRequest(
                            prompt=prompt,
                            invocation_dir=invocation_dir,
                            continuation=agent_runtime.Continuation(
                                serialized=role_session.read_continuation()
                            ),
                            provider_auth=provider_auth,
                            session_store=provider_state_dir,
                            timeout_seconds=self._cfg.idle_timeout,
                            on_live_output=_on_live_output,
                            token=cast(Any, request.token),
                            argv_transform=runner.provider_argv_transform(),
                        )
                    )
                else:
                    outcome = await runtime_client.run_new_session(
                        NewSessionRunRequest(
                            prompt=prompt,
                            invocation_dir=invocation_dir,
                            provider_selection=agent_runtime.ProviderSelection(
                                service=request.service,
                                model=resolved_model,
                                effort=resolved_effort,
                                auth=provider_auth,
                            ),
                            tool_policy=_runtime_tool_policy_for_role(request.role),
                            session_store=provider_state_dir,
                            timeout_seconds=self._cfg.idle_timeout,
                            name=request.name,
                            status_display=request.status_display,
                            work_body=request.work_body,
                            token=cast(Any, request.token),
                            on_live_output=_on_live_output,
                            argv_transform=runner.provider_argv_transform(),
                        )
                    )
                if not logged_lines[0] and outcome.result.output:
                    work_invocation_output = (
                        outcome.result.output
                        if outcome.result.output.endswith("\n")
                        else f"{outcome.result.output}\n"
                    )
                    work_invocation.append_provider_chunk(
                        work_invocation_output.encode()
                    )
            finally:
                runner._current_work_invocation = None
        return outcome

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
