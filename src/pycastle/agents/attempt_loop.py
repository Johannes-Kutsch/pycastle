from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from agent_runtime.contracts import ToolPolicy as RuntimeToolPolicy
from agent_runtime.errors import (
    ProviderUnavailableReason,
)
from agent_runtime.runtime import (
    Cancelled,
    Completed,
    ModelNotAvailable,
    ProviderUnavailable,
    TimedOut,
    UsageLimited,
)

from pycastle.agents.output_protocol import (
    AgentOutput,
    AgentOutputProtocolError,
    AgentRole,
    extract_output,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

    from agent_runtime.types import ResolvedProvider

    from pycastle.errors import TransientAgentError

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
    reset_time: datetime | None
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
    mount_path: Path
    session_namespace: str
    service_name: str
    session_store: Path
    log_path: Path | None


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
    protocol_reprompt_plan: Callable[[str | None], str],
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
            message = protocol_reprompt_plan(str(exc))
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
