from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, cast

import agent_runtime
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

from pycastle import stage_registry
from pycastle.agents import protocol_reprompt
from pycastle.agents.output_protocol import (
    AgentOutput,
    AgentOutputProtocolError,
    AgentRole,
    CompletionOutput,
    extract_output,
)
from pycastle.errors import (
    AgentFailedError,
    AgentTimeoutError,
    ModelNotAvailableError,
    TransientAgentError,
    UsageLimitError,
)
from pycastle.prompts.dispatch import PromptInvocation
from pycastle.prompts.scope_args import build_interrupted_work_clause
from pycastle.services.runtime_services import KNOWN_SERVICE_NAMES
from pycastle.session import RoleSession, RunKind
from pycastle.session.service_session_store import ServiceSessionStore

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agent_runtime.types import ResolvedProvider

    from pycastle.agents.runner import RunRequest
    from pycastle.services.runtime_services import AgentService

_MAX_PROTOCOL_RETRIES = 2


# ---------------------------------------------------------------------------
# Loop-directive closed union
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _ReturnParsed:
    parsed: AgentOutput
    clear_completion: bool


@dataclasses.dataclass
class _ReturnCancelled:
    pass


@dataclasses.dataclass
class _RaiseUsageLimit:
    reset_time: Any
    provider: str
    is_permanent: bool


@dataclasses.dataclass
class _RaiseTransientError:
    detail: str | None


@dataclasses.dataclass
class _RaiseProviderUsageLimit:
    provider: str
    raw_message: str | None


@dataclasses.dataclass
class _ResumeAfterTimeout:
    restart_num: int


@dataclasses.dataclass
class _RaiseTimeout:
    role_value: str


@dataclasses.dataclass
class _RaiseModelNotAvailable:
    service: str
    model: str
    stage_key: str | None


@dataclasses.dataclass
class _Reprompt:
    message: str


@dataclasses.dataclass
class _RaiseAgentFailed:
    role_value: str
    mount_path: Any  # Path
    session_namespace: str
    service_name: str
    session_store: Any  # Path
    log_path: Any  # Path | None


type _LoopDirective = (
    _ReturnParsed
    | _ReturnCancelled
    | _RaiseUsageLimit
    | _RaiseTransientError
    | _RaiseProviderUsageLimit
    | _ResumeAfterTimeout
    | _RaiseTimeout
    | _RaiseModelNotAvailable
    | _Reprompt
    | _RaiseAgentFailed
)

type _RuntimeOutcomeKind = (
    Cancelled
    | Completed
    | UsageLimited
    | ProviderUnavailable
    | TimedOut
    | ModelNotAvailable
)


def _decide_transition(  # noqa: PLR0913
    outcome_kind: _RuntimeOutcomeKind,
    *,
    attempt: int,
    retries_left: int,
    timeout_retries: int,
    selected: ResolvedProvider,
    output_text: str,
    role: AgentRole,
    protocol_reprompt_plan: Callable[
        [str | None], protocol_reprompt.ProtocolRepromptPlan
    ],
    preserve_session_on_completion: bool,
    role_value: str,
    mount_path: Path,
    session_namespace: str,
    service_name: str,
    session_store: Path,
    log_path: Path | None,
    stage_key: str | None,
) -> _LoopDirective:
    if isinstance(outcome_kind, Cancelled):
        return _ReturnCancelled()

    if isinstance(outcome_kind, Completed):
        try:
            parsed = extract_output(output_text, role)
        except AgentOutputProtocolError as exc:
            if attempt == _MAX_PROTOCOL_RETRIES:
                return _RaiseAgentFailed(
                    role_value=role_value,
                    mount_path=mount_path,
                    session_namespace=session_namespace,
                    service_name=service_name,
                    session_store=session_store,
                    log_path=log_path,
                )
            reprompt = protocol_reprompt_plan(str(exc))
            message = (
                protocol_reprompt.GENERIC_PROTOCOL_REPROMPT_MESSAGE
                if isinstance(reprompt, protocol_reprompt.UnsupportedProtocolReprompt)
                else reprompt.message
            )
            return _Reprompt(message=message)
        return _ReturnParsed(
            parsed=parsed,
            clear_completion=not preserve_session_on_completion,
        )

    if isinstance(outcome_kind, UsageLimited):
        return _RaiseUsageLimit(
            reset_time=outcome_kind.reset_time,
            provider=selected.service,
            is_permanent=outcome_kind.is_permanent,
        )

    if isinstance(outcome_kind, ProviderUnavailable):
        if outcome_kind.reason is ProviderUnavailableReason.TRANSIENT_API_ERROR:
            return _RaiseTransientError(detail=outcome_kind.detail)
        return _RaiseProviderUsageLimit(
            provider=selected.service,
            raw_message=outcome_kind.detail,
        )

    if isinstance(outcome_kind, TimedOut):
        if retries_left <= 0:
            return _RaiseTimeout(role_value=role_value)
        return _ResumeAfterTimeout(restart_num=timeout_retries - retries_left + 1)

    if isinstance(outcome_kind, ModelNotAvailable):
        return _RaiseModelNotAvailable(
            service=selected.service,
            model=selected.model,
            stage_key=stage_key,
        )

    raise AssertionError(
        f"Unknown runtime outcome kind: {type(outcome_kind).__name__!r}"
    )


def format_transient_status_message(err: TransientAgentError) -> str:
    detail = str(err)
    return f"transient API error: {detail}" if detail else "transient API error"


def _runtime_tool_policy_for_role(role: AgentRole) -> RuntimeToolPolicy:
    if role is AgentRole.PLANNER:
        return RuntimeToolPolicy.NO_FILE_MUTATION
    return RuntimeToolPolicy.UNRESTRICTED


@dataclasses.dataclass
class _AttemptLoopBundle:
    service: AgentService
    runner: Any  # ContainerRunner
    runtime_client: Any  # agent_runtime.RuntimeClient or compatible duck-typed object
    role_session: RoleSession
    provider_state_dir: Path
    provider_auth: Any  # ProviderAuth | None
    resolved_model: str
    resolved_effort: str
    status_display: Any  # StatusDisplay
    protocol_reprompt_plan: Callable[
        [str | None], protocol_reprompt.ProtocolRepromptPlan
    ]
    render_prompt: Callable[..., Any]  # async (request, run_kind) -> str
    handle_provider_account_exhaustion: Callable[[AgentService, UsageLimitError], None]
    is_working_tree_clean: Callable[[Path], bool]
    timeout_retries: int
    idle_timeout: int


async def run_attempt_loop(
    request: RunRequest,
    bundle: _AttemptLoopBundle,
) -> AgentOutput:
    """Pycastle-side attempt loop. Returns AgentOutput; raises on unrecoverable failure."""
    current_prompt = await bundle.render_prompt(request, bundle.role_session.run_kind())
    current_run_kind = bundle.role_session.run_kind()
    retries_left = bundle.timeout_retries

    for attempt in range(3):
        _saved_service = (
            ServiceSessionStore(bundle.role_session.path).transcript_owner_service_name(
                KNOWN_SERVICE_NAMES
            )
            if current_run_kind is RunKind.RESUME and bundle.role_session.is_resumable()
            else None
        )
        if _saved_service is not None and _saved_service != request.service:
            request, current_prompt = await _recover_stale_continuation(
                request=request,
                bundle=bundle,
            )
            current_run_kind = RunKind.FRESH

        try:
            outcome = await _run_runtime_once(
                request=request,
                bundle=bundle,
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
            request, current_prompt = await _recover_stale_continuation(
                request=request,
                bundle=bundle,
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
            bundle.role_session.write_continuation(continuation.serialized)

        directive = _decide_transition(
            outcome.kind,
            attempt=attempt,
            retries_left=retries_left,
            timeout_retries=bundle.timeout_retries,
            selected=outcome.result.selected,
            output_text=outcome.result.output or "",
            role=request.role,
            protocol_reprompt_plan=bundle.protocol_reprompt_plan,
            preserve_session_on_completion=request.preserve_session_on_completion,
            role_value=request.role.value,
            mount_path=request.mount_path,
            session_namespace=request.session_namespace,
            service_name=bundle.service.name,
            session_store=bundle.role_session.path,
            log_path=getattr(bundle.runner, "log_path", None),
            stage_key=stage_registry.stage_key_for_role(request.role),
        )

        match directive:
            case _ReturnCancelled():
                return CompletionOutput()
            case _ReturnParsed(parsed=p, clear_completion=clear):
                if clear:
                    bundle.role_session.clear_provider_state_and_signal_completion()
                return p
            case _RaiseUsageLimit(reset_time=rt, provider=prov, is_permanent=perm):
                error = UsageLimitError(reset_time=rt, provider=prov, is_permanent=perm)
                bundle.handle_provider_account_exhaustion(bundle.service, error)
                raise error
            case _RaiseTransientError(detail=detail):
                transient_err = TransientAgentError(message=detail or "")
                bundle.status_display.print(
                    request.name, format_transient_status_message(transient_err)
                )
                raise transient_err
            case _RaiseProviderUsageLimit(provider=prov, raw_message=msg):
                error = UsageLimitError(provider=prov, raw_message=msg)
                bundle.handle_provider_account_exhaustion(bundle.service, error)
                raise error
            case _ResumeAfterTimeout(restart_num=n):
                bundle.status_display.print(
                    request.name,
                    f"Timeout — restarting (attempt {n}/{bundle.timeout_retries})",
                )
                current_run_kind = RunKind.RESUME
                current_prompt = await bundle.render_prompt(request, current_run_kind)
                retries_left -= 1
                continue
            case _RaiseTimeout(role_value=rv):
                raise AgentTimeoutError("Provider timed out", role_value=rv)
            case _RaiseModelNotAvailable(service=svc, model=mdl, stage_key=sk):
                bundle.service.mark_model_restricted(mdl)
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


async def _recover_stale_continuation(
    *,
    request: RunRequest,
    bundle: _AttemptLoopBundle,
) -> tuple[RunRequest, str]:
    bundle.role_session.start_fresh()
    is_dirty = not bundle.is_working_tree_clean(request.mount_path)
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
    new_prompt = await bundle.render_prompt(request, RunKind.FRESH)
    return request, new_prompt


async def _run_runtime_once(
    *,
    request: RunRequest,
    bundle: _AttemptLoopBundle,
    prompt: str,
    run_kind: RunKind,
) -> Any:  # noqa: ANN401  # returns agent_runtime.RuntimeOutcome or compatible duck-typed object
    invocation_dir = request.mount_path
    logged_lines = [False]

    def _on_live_output(event: agent_runtime.AgentEvent) -> None:
        if bundle.runner.on_live_output(event):
            logged_lines[0] = True

    with bundle.runner.open_work_invocation(
        role=request.role,
        run_kind=run_kind,
        session_uuid=None,
        prompt=prompt,
    ):
        if run_kind is RunKind.RESUME and bundle.role_session.is_resumable():
            outcome = await bundle.runtime_client.run_resumed_session(
                ResumedSessionRunRequest(
                    prompt=prompt,
                    invocation_dir=invocation_dir,
                    continuation=agent_runtime.Continuation(
                        serialized=bundle.role_session.read_continuation()
                    ),
                    provider_auth=bundle.provider_auth,
                    session_store=bundle.provider_state_dir,
                    timeout_seconds=bundle.idle_timeout,
                    on_live_output=_on_live_output,
                    token=cast("Any", request.token),
                    argv_transform=bundle.runner.provider_argv_transform(),
                )
            )
        else:
            outcome = await bundle.runtime_client.run_new_session(
                NewSessionRunRequest(
                    prompt=prompt,
                    invocation_dir=invocation_dir,
                    provider_selection=agent_runtime.ProviderSelection(
                        service=request.service,
                        model=bundle.resolved_model,
                        effort=bundle.resolved_effort,
                        auth=bundle.provider_auth,
                    ),
                    tool_policy=_runtime_tool_policy_for_role(request.role),
                    session_store=bundle.provider_state_dir,
                    timeout_seconds=bundle.idle_timeout,
                    name=request.name,
                    status_display=request.status_display,
                    work_body=request.work_body,
                    token=cast("Any", request.token),
                    on_live_output=_on_live_output,
                    argv_transform=bundle.runner.provider_argv_transform(),
                )
            )
        if not logged_lines[0] and outcome.result.output:
            bundle.runner.append_chunk(outcome.result.output)
    return outcome
