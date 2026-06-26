from __future__ import annotations

import dataclasses
from pathlib import Path

from ..agents.output_protocol import AgentRole
from ..services.agent_service import AgentService
from pycastle.runtime_session import RunKind
from .agent import (
    AuthSeedingRequirement,
    LocalAuthSeedAction,
    RunSessionPlan,
    RunSessionPlanRequest,
    plan_run_session,
)
from .agent._planning import run_session_plan_from_provider_run_state_plan
from .run_dispatch import (
    AgentRunSessionState,
    AgentRunSessionStateRequest,
    PreparedAgentProviderRunSession,
    prepare_agent_run_session_state,
)
from .run_state_plan import (
    ProviderRunStatePlan,
    ProviderRunStatePlanRequest,
    plan_provider_run_state,
)


@dataclasses.dataclass(frozen=True)
class ProviderSessionStateRequest:
    worktree: Path
    role: AgentRole
    session_namespace: str
    service: AgentService
    provider_run_state_plan: ProviderRunStatePlan | None = None
    require_exact_transcript_for_strict_resume: bool = False


@dataclasses.dataclass
class PreparedProviderSessionState:
    role_session: object
    run_kind: RunKind
    provider_session_id: str | None
    service_state_dir_relpath: str | None
    service_state_dir_path: Path | None
    auth_seeding_requirement: AuthSeedingRequirement
    worktree: Path = dataclasses.field(repr=False)
    role: AgentRole = dataclasses.field(repr=False)
    session_namespace: str = dataclasses.field(repr=False)
    service: AgentService = dataclasses.field(repr=False)
    _state: AgentRunSessionState = dataclasses.field(repr=False)
    auth_seed_action: LocalAuthSeedAction | None = None
    exact_transcript_match: bool = False

    @property
    def provider_state_dir_relpath(self) -> str | None:
        return self.service_state_dir_relpath

    def provider_state_dir_container_path(self, container_workspace: str) -> str | None:
        return self._state.provider_state_dir_container_path(container_workspace)

    def initial_provider_run_session(self) -> PreparedAgentProviderRunSession:
        return self._state.initial_provider_run_session()

    def resumable_provider_run_session(self) -> PreparedAgentProviderRunSession:
        return self._state.resumable_provider_run_session()

    def protocol_reprompt_provider_run_session(
        self,
    ) -> PreparedAgentProviderRunSession | None:
        return self._state.protocol_reprompt_provider_run_session()

    def prepare_for_run(self) -> None:
        self._state.prepare_for_run()

    def record_provider_session_id(self, provider_session_id: str) -> None:
        self._state.record_provider_session_id(provider_session_id)
        self.provider_session_id = self._state.provider_session_id

    def record_successful_run(self) -> None:
        self._state.record_successful_run()


def prepare_provider_session_state(
    request: ProviderSessionStateRequest,
) -> PreparedProviderSessionState:
    run_session_plan = _run_session_plan_for_request(request)
    state = prepare_agent_run_session_state(
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
    return PreparedProviderSessionState(
        role_session=state.role_session,
        run_kind=state.run_kind,
        provider_session_id=state.provider_session_id,
        service_state_dir_relpath=state.service_state_dir_relpath,
        service_state_dir_path=state.service_state_dir_path,
        auth_seeding_requirement=run_session_plan.auth_seeding_requirement,
        worktree=request.worktree,
        role=request.role,
        session_namespace=request.session_namespace,
        service=request.service,
        auth_seed_action=state.auth_seed_action,
        exact_transcript_match=state.exact_transcript_match,
        _state=state,
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


def has_exact_transcript_match(
    *,
    worktree: Path,
    role: AgentRole,
    session_namespace: str,
    service: AgentService,
) -> bool:
    return plan_provider_run_state(
        ProviderRunStatePlanRequest(
            worktree=worktree,
            role=role,
            namespace=session_namespace,
            service=service,
        )
    ).exact_transcript_match


__all__ = [
    "PreparedProviderSessionState",
    "ProviderSessionStateRequest",
    "prepare_provider_session_state",
    "has_exact_transcript_match",
]
