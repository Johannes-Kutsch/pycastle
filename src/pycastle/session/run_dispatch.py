from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, cast

from pycastle.provider_session_adapter import provider_session_adapter_for_service
from pycastle.runtime_session import (
    ProviderSessionState,
    ProviderSessionStateRequest,
    RunKind,
)
from pycastle.session.agent import (
    AuthSeedingRequirement,
    LocalAuthSeedAction,
    RunSessionPlan,
    RunSessionPlanRequest,
)
from pycastle.session.agent._planning import plan_run_session
from pycastle.session.role import RoleSession
from pycastle.session.service_session_store import ServiceSessionStore
from pycastle.session_planning import (
    ProviderRunStatePlanRequest,
    plan_provider_run_state,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pycastle.agents.output_protocol import AgentRole
    from pycastle.services.runtime_services import AgentService


@dataclasses.dataclass(frozen=True)
class AgentRunSessionStateRequest:
    worktree: Path
    role: AgentRole
    session_namespace: str
    service: AgentService
    run_session_plan: RunSessionPlan | None = None
    require_exact_transcript_for_strict_resume: bool = False


@dataclasses.dataclass
class PreparedAgentProviderRunSession:
    run_kind: RunKind
    provider_session_id: str | None
    _state: AgentRunSessionState = dataclasses.field(repr=False, compare=False)

    def record_provider_session_id(self, provider_session_id: str) -> None:
        self.provider_session_id = provider_session_id
        self._state.record_provider_session_id(provider_session_id)

    def record_successful_run(self) -> None:
        self._state.record_successful_run()


@dataclasses.dataclass
class AgentRunSessionState:
    role_session: RoleSession
    run_kind: RunKind
    provider_session_id: str | None
    service_state_dir_relpath: str | None
    service_state_dir_path: Path | None
    _plan: RunSessionPlan = dataclasses.field(repr=False)
    provider_state_dir_container_path: str | None = None
    auth_seed_action: LocalAuthSeedAction | None = None
    exact_transcript_match: bool = False
    require_exact_transcript_for_strict_resume: bool = False
    _observed_provider_session_id: bool = dataclasses.field(
        default=False,
        repr=False,
    )

    @property
    def auth_seeding_requirement(self) -> AuthSeedingRequirement:
        return self._plan.auth_seeding_requirement

    @property
    def provider_state_dir_relpath(self) -> str | None:
        return self.service_state_dir_relpath

    @property
    def codex_auth_seed_input(self) -> Path | None:
        if self.auth_seed_action is None:
            return None
        return self.auth_seed_action.source

    def compute_container_path(self, container_workspace: str) -> str | None:
        return self._plan.provider_state_dir_container_path(container_workspace)

    def initial_provider_run_session(self) -> PreparedAgentProviderRunSession:
        return PreparedAgentProviderRunSession(
            run_kind=self.run_kind,
            provider_session_id=self.provider_session_id,
            _state=self,
        )

    def resumable_provider_run_session(self) -> PreparedAgentProviderRunSession:
        provider_session_state = self._resume_provider_session_state()
        return PreparedAgentProviderRunSession(
            run_kind=provider_session_state.run_kind,
            provider_session_id=provider_session_state.provider_session_id,
            _state=self,
        )

    def protocol_reprompt_provider_run_session(
        self,
    ) -> PreparedAgentProviderRunSession | None:
        provider_session_state = self._resume_provider_session_state()
        if not provider_session_state.allow_protocol_reprompt:
            return None
        return PreparedAgentProviderRunSession(
            run_kind=provider_session_state.run_kind,
            provider_session_id=provider_session_state.provider_session_id,
            _state=self,
        )

    def prepare_for_run(self) -> None:
        if self.auth_seed_action is not None:
            self.auth_seed_action.require_source()
        preserved_auth = self._preserved_codex_auth_bytes()
        if self.run_kind is RunKind.FRESH:
            self.role_session.start_fresh()
            if preserved_auth is not None:
                auth_path = self._codex_auth_path()
                if auth_path is not None:
                    auth_path.parent.mkdir(parents=True, exist_ok=True)
                    auth_path.write_bytes(preserved_auth)
        self._plan.prepare_host_provider_state_dir()

    def record_provider_session_id(self, provider_session_id: str) -> None:
        self.provider_session_id = provider_session_id
        self._observed_provider_session_id = True
        self._plan.capture_provider_session_id(provider_session_id)

    def record_successful_run(self) -> None:
        self._plan.record_successful_run(self.provider_session_id)

    def _preserved_codex_auth_bytes(self) -> bytes | None:
        auth_path = self._codex_auth_path()
        if auth_path is None or not auth_path.is_file():
            return None
        return auth_path.read_bytes()

    def _codex_auth_path(self) -> Path | None:
        if self._plan.service.name != "codex":
            return None
        host_provider_state_dir = self._plan.host_provider_state_dir
        if host_provider_state_dir is None:
            return None
        return host_provider_state_dir / "auth.json"

    def _resume_provider_session_state(self) -> ProviderSessionState:
        if self.provider_session_id is not None and (
            self._observed_provider_session_id
            or self.exact_transcript_match
            or not self.require_exact_transcript_for_strict_resume
        ):
            return ProviderSessionState(
                run_kind=RunKind.RESUME,
                provider_session_id=self.provider_session_id,
                state_dir_relpath=self.provider_state_dir_relpath,
                state_dir_path=self.service_state_dir_path,
                exact_transcript_match=self.exact_transcript_match,
            )
        if (
            self.require_exact_transcript_for_strict_resume
            and self.provider_session_id is not None
            and not self.exact_transcript_match
        ):
            return ProviderSessionState(
                run_kind=RunKind.FRESH,
                provider_session_id=None,
                state_dir_relpath=self.provider_state_dir_relpath,
                state_dir_path=self.service_state_dir_path,
                allow_protocol_reprompt=False,
            )
        return self._plan.service.provider_session_state(
            ProviderSessionStateRequest(
                role_session=ServiceSessionStore(
                    self.role_session.path, self.role_session
                ),
                provider_state_dir=self.service_state_dir_path,
                has_resumable_provider_state=(
                    self.service_state_dir_path is not None
                    and self._plan.service.is_resumable(self.service_state_dir_path)
                ),
                state_dir_relpath=self.provider_state_dir_relpath,
                preferred_provider_session_id=self.provider_session_id,
                force_resume=True,
            )
        )


@dataclasses.dataclass(frozen=True)
class RunSessionRequest:
    worktree: Path
    role: AgentRole
    session_namespace: str
    service: AgentService
    container_workspace: str
    run_session_plan: RunSessionPlan | None = None
    require_exact_transcript_for_strict_resume: bool = False


def prepare_agent_run_session_state(
    request: AgentRunSessionStateRequest,
) -> AgentRunSessionState:
    plan = request.run_session_plan or plan_run_session(
        RunSessionPlanRequest(
            role=request.role,
            worktree=request.worktree,
            namespace=request.session_namespace,
            service=request.service,
        )
    )
    auth_seed_action = cast("LocalAuthSeedAction | None", plan.auth_seed_action)
    if auth_seed_action is not None:
        auth_seed_action.require_source()
    role_session = RoleSession(
        request.worktree,
        request.role,
        request.session_namespace,
    )
    provider_session_id = plan.prepared_provider_session_id()
    return AgentRunSessionState(
        role_session=role_session,
        run_kind=plan.run_kind,
        provider_session_id=provider_session_id,
        service_state_dir_relpath=plan.provider_state_dir_relpath,
        service_state_dir_path=plan.host_provider_state_dir,
        auth_seed_action=auth_seed_action,
        exact_transcript_match=plan.exact_transcript_match,
        require_exact_transcript_for_strict_resume=(
            request.require_exact_transcript_for_strict_resume
        ),
        _plan=plan,
    )


def record_observed_provider_session_id(
    session_state: AgentRunSessionState,
    provider_session_id: str,
) -> None:
    session_state.record_provider_session_id(provider_session_id)


def record_successful_provider_session_metadata(
    session_state: AgentRunSessionState,
) -> None:
    session_state.record_successful_run()


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
            role_session=ServiceSessionStore(
                RoleSession(worktree, role, session_namespace).path
            ),
            provider_session_adapter=provider_session_adapter_for_service(service),
        )
    ).exact_transcript_match


def prepare_run_session(request: RunSessionRequest) -> AgentRunSessionState:
    session_state = prepare_agent_run_session_state(
        AgentRunSessionStateRequest(
            worktree=request.worktree,
            role=request.role,
            session_namespace=request.session_namespace,
            service=request.service,
            run_session_plan=request.run_session_plan,
            require_exact_transcript_for_strict_resume=(
                request.require_exact_transcript_for_strict_resume
            ),
        )
    )
    session_state.provider_state_dir_container_path = (
        session_state.compute_container_path(request.container_workspace)
    )
    return session_state


__all__ = [
    "AgentRunSessionState",
    "AgentRunSessionStateRequest",
    "PreparedAgentProviderRunSession",
    "RunSessionRequest",
    "has_exact_transcript_match",
    "prepare_agent_run_session_state",
    "prepare_run_session",
    "record_observed_provider_session_id",
    "record_successful_provider_session_metadata",
]
