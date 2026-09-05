from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from pycastle.runtime_session import RunKind
from pycastle.session.agent import (
    RunSessionPlan,
    RunSessionPlanRequest,
    plan_run_session,
    run_session_plan_from_provider_run_state_plan,
)
from pycastle.session.role import (
    SESSION_DIR_NAME,
    RoleSession,
    any_role_dir_present,
    is_stage_done_for,
    provider_state_relpath,
)
from pycastle.session.run_dispatch import (
    AgentRunSessionState,
    AgentRunSessionStateRequest,
    PreparedAgentProviderRunSession,
    RunSessionRequest,
    has_exact_transcript_match,
    prepare_agent_run_session_state,
    prepare_run_session,
)
from pycastle.session.run_state import ProviderFreshFallbackReason, ProviderRunState

if TYPE_CHECKING:
    from pathlib import Path

    from pycastle.agents.output_protocol import AgentRole
    from pycastle.services.runtime_services import AgentService
    from pycastle.session_planning import ProviderRunStatePlan


@dataclasses.dataclass(frozen=True)
class ProviderSessionStateRequest:
    worktree: Path
    role: AgentRole
    session_namespace: str
    service: AgentService
    provider_run_state_plan: ProviderRunStatePlan | None = None
    require_exact_transcript_for_strict_resume: bool = False


def prepare_provider_session_state(
    request: ProviderSessionStateRequest,
) -> AgentRunSessionState:
    run_session_plan = _run_session_plan_for_request(request)
    return prepare_agent_run_session_state(
        AgentRunSessionStateRequest(
            worktree=request.worktree,
            role=request.role,
            session_namespace=request.session_namespace,
            service=request.service,
            run_session_plan=run_session_plan,
            require_exact_transcript_for_strict_resume=(
                request.require_exact_transcript_for_strict_resume
            ),
        )
    )


def _run_session_plan_for_request(
    request: ProviderSessionStateRequest,
) -> RunSessionPlan:
    provider_run_state_plan = request.provider_run_state_plan
    if provider_run_state_plan is None:
        return plan_run_session(
            RunSessionPlanRequest(
                role=request.role,
                worktree=request.worktree,
                namespace=request.session_namespace,
                service=request.service,
            )
        )
    return run_session_plan_from_provider_run_state_plan(
        role=request.role,
        worktree=request.worktree,
        namespace=request.session_namespace,
        service=request.service,
        provider_run_state_plan=provider_run_state_plan,
    )


__all__ = [
    "SESSION_DIR_NAME",
    "AgentRunSessionState",
    "AgentRunSessionStateRequest",
    "PreparedAgentProviderRunSession",
    "ProviderFreshFallbackReason",
    "ProviderRunState",
    "ProviderSessionStateRequest",
    "RoleSession",
    "RunKind",
    "RunSessionRequest",
    "any_role_dir_present",
    "has_exact_transcript_match",
    "is_stage_done_for",
    "prepare_agent_run_session_state",
    "prepare_provider_session_state",
    "prepare_run_session",
    "provider_state_relpath",
]
