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
from pycastle.session import RoleSession, RunKind
from pycastle.session.service_session_store import ServiceSessionStore

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pycastle.agents.runner import RunRequest
    from pycastle.services.runtime_services import AgentService

_MAX_PROTOCOL_RETRIES = 2


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
            ServiceSessionStore(
                bundle.role_session.path
            ).exact_transcript_service_name()
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

        if isinstance(outcome.kind, Cancelled):
            return CompletionOutput()

        if isinstance(outcome.kind, Completed):
            try:
                parsed = extract_output(outcome.result.output, request.role)
            except AgentOutputProtocolError as exc:
                if attempt == _MAX_PROTOCOL_RETRIES:
                    raise AgentFailedError(
                        role_value=request.role.value,
                        worktree_path=request.mount_path,
                        namespace=request.session_namespace,
                        failure_class="protocol_error",
                        service_name=bundle.service.name,
                        session_store=bundle.role_session.path,
                        agent_invocation_log_path=getattr(
                            bundle.runner, "log_path", None
                        ),
                    ) from exc
                reprompt = bundle.protocol_reprompt_plan(str(exc))
                current_prompt = (
                    protocol_reprompt.GENERIC_PROTOCOL_REPROMPT_MESSAGE
                    if isinstance(
                        reprompt, protocol_reprompt.UnsupportedProtocolReprompt
                    )
                    else reprompt.message
                )
                current_run_kind = RunKind.RESUME
                continue
            if not request.preserve_session_on_completion:
                bundle.role_session.clear_provider_state_and_signal_completion()
            return parsed

        if isinstance(outcome.kind, UsageLimited):
            error = UsageLimitError(
                reset_time=outcome.kind.reset_time,
                provider=outcome.result.selected.service,
                is_permanent=outcome.kind.is_permanent,
            )
            bundle.handle_provider_account_exhaustion(bundle.service, error)
            raise error

        if isinstance(outcome.kind, ProviderUnavailable):
            if outcome.kind.reason is ProviderUnavailableReason.TRANSIENT_API_ERROR:
                bundle.status_display.print(
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
            bundle.handle_provider_account_exhaustion(bundle.service, error)
            raise error

        if isinstance(outcome.kind, TimedOut):
            if retries_left <= 0:
                raise AgentTimeoutError(
                    "Provider timed out",
                    role_value=request.role.value,
                )
            restart_num = bundle.timeout_retries - retries_left + 1
            bundle.status_display.print(
                request.name,
                f"Timeout — restarting (attempt {restart_num}/{bundle.timeout_retries})",
            )
            current_run_kind = RunKind.RESUME
            current_prompt = await bundle.render_prompt(request, current_run_kind)
            retries_left -= 1
            continue

        if isinstance(outcome.kind, ModelNotAvailable):
            model = outcome.result.selected.model
            bundle.service.mark_model_restricted(model)
            raise ModelNotAvailableError(
                service=outcome.result.selected.service,
                model=model,
                stage_key=_stage_key_for_role(request.role),
            )

        raise RuntimeError("Unexpected runtime outcome kind")

    raise RuntimeError("Runtime reprompt loop exhausted unexpectedly")


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
