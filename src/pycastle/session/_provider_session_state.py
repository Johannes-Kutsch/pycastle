from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..agents.output_protocol import AgentRole
from ._agent_run_session_state import (
    AgentRunSessionState,
    AgentRunSessionStateRequest,
    PreparedAgentProviderRunSession,
    prepare_agent_run_session_state,
)
from ._provider_session_decision import (
    AuthSeedingRequirement,
    LocalAuthSeedAction,
    ProviderSessionDecision,
)
from ._provider_session_plan import (
    ProviderRunStatePlan,
    ProviderRunStatePlanRequest,
    plan_provider_run_state,
)
from .agent import RunSessionPlan, RunSessionPlanRequest, plan_run_session
from .resume import RoleSession, RunKind

if TYPE_CHECKING:
    from ..services.agent_service import AgentService


@dataclasses.dataclass(frozen=True)
class ProviderSessionStateRequest:
    worktree: Path
    role: AgentRole
    session_namespace: str
    service: AgentService
    provider_run_state_plan: ProviderRunStatePlan | None = None
    provider_session_decision: ProviderSessionDecision | None = None


@dataclasses.dataclass
class PreparedProviderSessionState:
    role_session: RoleSession
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

    def initial_provider_run_session(self) -> PreparedProviderRunSession:
        return _wrap_provider_run_session(self._state.initial_provider_run_session())

    def resumable_provider_run_session(self) -> PreparedProviderRunSession:
        return _wrap_provider_run_session(self._state.resumable_provider_run_session())

    def protocol_reprompt_provider_run_session(
        self,
    ) -> PreparedProviderRunSession | None:
        state_run_session = self._state.protocol_reprompt_provider_run_session()
        if state_run_session is None:
            return None
        return _wrap_provider_run_session(state_run_session)

    def prepare_for_run(self) -> None:
        self._state.prepare_for_run()

    def record_provider_session_id(self, provider_session_id: str) -> None:
        self._state.record_provider_session_id(provider_session_id)
        self.provider_session_id = self._state.provider_session_id

    def record_successful_run(self) -> None:
        self._state.record_successful_run()


@dataclasses.dataclass(frozen=True)
class PreparedProviderRunSession:
    run_kind: RunKind
    provider_session_id: str | None
    _provider_session_id_recorder: Callable[[str], None] | None = dataclasses.field(
        default=None,
        repr=False,
        compare=False,
    )
    _success_recorder: Callable[[], None] | None = dataclasses.field(
        default=None,
        repr=False,
        compare=False,
    )

    def record_provider_session_id(self, provider_session_id: str) -> None:
        object.__setattr__(self, "provider_session_id", provider_session_id)
        if self._provider_session_id_recorder is not None:
            self._provider_session_id_recorder(provider_session_id)

    def record_successful_run(self) -> None:
        if self._success_recorder is not None:
            self._success_recorder()


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
        decision = request.provider_session_decision
        if decision is not None:
            provider_run_state_plan = ProviderRunStatePlan(
                role_session=RoleSession(
                    request.worktree,
                    request.role,
                    request.session_namespace,
                ),
                service_name=request.service.name,
                run_kind=decision.run_kind,
                provider_state_dir=decision.state_dir_path,
                provider_state_dir_relpath=decision.state_dir_relpath,
                provider_session_id=decision.provider_session_id,
                requires_host_codex_auth=(
                    decision.auth_seeding_requirement is AuthSeedingRequirement.REQUIRED
                ),
                recovered_session_id_persistence=(
                    decision.recovered_session_id_persistence
                ),
                service_state_dir=decision.service_state_dir,
                exact_transcript_match=decision.exact_transcript_match,
                auth_seed_action=decision.auth_seed_action,
            )
        else:
            return plan_run_session(
                RunSessionPlanRequest(
                    role=request.role,
                    worktree=request.worktree,
                    namespace=request.session_namespace,
                    service=request.service,
                )
            )

    return RunSessionPlan(
        role=request.role,
        worktree=request.worktree,
        namespace=request.session_namespace,
        service=request.service,
        provider_session_plan=provider_run_state_plan.provider_session_decision(),
        run_kind=provider_run_state_plan.run_kind,
        service_state_dir=provider_run_state_plan.service_state_dir,
        provider_state_dir_relpath=provider_run_state_plan.provider_state_dir_relpath,
        host_provider_state_dir=provider_run_state_plan.provider_state_dir,
        provider_session_id=provider_run_state_plan.provider_session_id,
        auth_seeding_requirement=provider_run_state_plan.auth_seeding_requirement,
        recovered_session_id_persistence=(
            provider_run_state_plan.recovered_session_id_persistence
        ),
        auth_seed_action=provider_run_state_plan.auth_seed_action,
        exact_transcript_match=provider_run_state_plan.exact_transcript_match,
        _provider_run_state_plan=provider_run_state_plan,
    )


def _wrap_provider_run_session(
    state_run_session: PreparedAgentProviderRunSession,
) -> PreparedProviderRunSession:
    return PreparedProviderRunSession(
        run_kind=state_run_session.run_kind,
        provider_session_id=state_run_session.provider_session_id,
        _provider_session_id_recorder=state_run_session._provider_session_id_recorder,
        _success_recorder=state_run_session._success_recorder,
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
    "PreparedProviderRunSession",
    "PreparedProviderSessionState",
    "ProviderSessionStateRequest",
    "prepare_provider_session_state",
    "has_exact_transcript_match",
]
