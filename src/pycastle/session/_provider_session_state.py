from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING
from collections.abc import Callable

from ..agents.output_protocol import AgentRole
from ..errors import HardAgentError
from ._provider_session_decision import (
    AuthSeedingRequirement,
    LocalAuthSeedAction,
    ProviderSessionDecision,
    RecoveredSessionIdPersistence,
)
from ._provider_session_plan import (
    capture_provider_session_id,
    ProviderSessionPlanRequest,
    plan_provider_session,
    record_successful_provider_session_metadata,
)
from .resume import RoleSession, RunKind

if TYPE_CHECKING:
    from ..services.agent_service import AgentService
from ..services.provider_session_state import (
    ProviderSessionStateRequest as ServiceProviderSessionStateRequest,
)


@dataclasses.dataclass(frozen=True)
class ProviderSessionStateRequest:
    worktree: Path
    role: AgentRole
    session_namespace: str
    service: AgentService
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
    _provider_session_decision: ProviderSessionDecision = dataclasses.field(repr=False)
    auth_seed_action: LocalAuthSeedAction | None = None
    exact_transcript_match: bool = False

    @property
    def provider_state_dir_relpath(self) -> str | None:
        return self.service_state_dir_relpath

    def provider_state_dir_container_path(self, container_workspace: str) -> str | None:
        return self._provider_session_decision.container_state_dir_path(
            worktree=self.worktree,
            service_name=self.service.name,
            container_workspace=container_workspace,
        )

    def initial_provider_run_session(self) -> PreparedProviderRunSession:
        return PreparedProviderRunSession(
            run_kind=self.run_kind,
            provider_session_id=self.provider_session_id,
            _provider_session_id_recorder=self.record_provider_session_id,
            _success_recorder=self.record_successful_run,
        )

    def resumable_provider_run_session(self) -> PreparedProviderRunSession:
        provider_session_state = self._resume_provider_session_state()
        return PreparedProviderRunSession(
            run_kind=provider_session_state.run_kind,
            provider_session_id=provider_session_state.provider_session_id,
            _provider_session_id_recorder=self.record_provider_session_id,
            _success_recorder=self.record_successful_run,
        )

    def protocol_reprompt_provider_run_session(
        self,
    ) -> PreparedProviderRunSession | None:
        provider_session_state = self._resume_provider_session_state()
        if not provider_session_state.allow_protocol_reprompt:
            return None
        return PreparedProviderRunSession(
            run_kind=provider_session_state.run_kind,
            provider_session_id=provider_session_state.provider_session_id,
            _provider_session_id_recorder=self.record_provider_session_id,
            _success_recorder=self.record_successful_run,
        )

    def prepare_for_run(self) -> None:
        _require_auth_seed_source(self.auth_seed_action)
        preserved_auth = self._preserved_codex_auth_bytes()
        if self.run_kind is RunKind.FRESH:
            self.role_session.start_fresh()
            if preserved_auth is not None:
                auth_path = self._codex_auth_path()
                if auth_path is not None:
                    auth_path.parent.mkdir(parents=True, exist_ok=True)
                    auth_path.write_bytes(preserved_auth)
        if self.service_state_dir_path is not None:
            self.service_state_dir_path.mkdir(parents=True, exist_ok=True)
        if self.auth_seed_action is not None:
            self.auth_seed_action.apply()

    def record_provider_session_id(self, provider_session_id: str) -> None:
        self.provider_session_id = provider_session_id
        capture_provider_session_id(
            worktree=self.worktree,
            role=self.role,
            namespace=self.session_namespace,
            service_name=self.service.name,
            service_state_dir=self._provider_session_decision.service_state_dir,
            provider_session_id=provider_session_id,
        )

    def record_successful_run(self) -> None:
        record_successful_provider_session_metadata(
            worktree=self.worktree,
            role=self.role,
            namespace=self.session_namespace,
            service_name=self.service.name,
            provider_session_id=self.provider_session_id,
        )

    def _preserved_codex_auth_bytes(self) -> bytes | None:
        auth_path = self._codex_auth_path()
        if auth_path is None or not auth_path.is_file():
            return None
        return auth_path.read_bytes()

    def _codex_auth_path(self) -> Path | None:
        if self.service.name != "codex":
            return None
        if self.service_state_dir_path is None:
            return None
        return self.service_state_dir_path / "auth.json"

    def _resume_provider_session_state(self):
        service_state = self.role_session.service_session_state(self.service)
        return self.service.provider_session_state(
            ServiceProviderSessionStateRequest(
                role_session=self.role_session,
                provider_state_dir=service_state.state_dir,
                has_resumable_provider_state=service_state.has_resumable_provider_state,
                preferred_provider_session_id=self.provider_session_id,
                force_resume=True,
            )
        )


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
    decision = request.provider_session_decision or plan_provider_session(
        ProviderSessionPlanRequest(
            worktree=request.worktree,
            role=request.role,
            namespace=request.session_namespace,
            service=request.service,
        )
    )
    auth_seed_action = decision.auth_seed_action
    if auth_seed_action is not None:
        auth_seed_action.require_source()
    role_session = RoleSession(
        request.worktree,
        request.role,
        request.session_namespace,
    )
    provider_session_id = decision.provider_session_id
    if (
        provider_session_id is not None
        and decision.recovered_session_id_persistence
        is RecoveredSessionIdPersistence.PERSIST
    ):
        capture_provider_session_id(
            worktree=request.worktree,
            role=request.role,
            namespace=request.session_namespace,
            service_name=request.service.name,
            service_state_dir=decision.service_state_dir,
            provider_session_id=provider_session_id,
        )
    return PreparedProviderSessionState(
        role_session=role_session,
        run_kind=decision.run_kind,
        provider_session_id=provider_session_id,
        service_state_dir_relpath=decision.state_dir_relpath,
        service_state_dir_path=decision.state_dir_path,
        auth_seeding_requirement=decision.auth_seeding_requirement,
        worktree=request.worktree,
        role=request.role,
        session_namespace=request.session_namespace,
        service=request.service,
        _provider_session_decision=decision,
        auth_seed_action=auth_seed_action,
        exact_transcript_match=decision.exact_transcript_match,
    )


def _require_auth_seed_source(
    auth_seed_action: LocalAuthSeedAction | None,
) -> None:
    if auth_seed_action is None or auth_seed_action.source.exists():
        return
    raise HardAgentError(
        auth_seed_action.missing_source_message,
        status_code=401,
    )


__all__ = [
    "PreparedProviderRunSession",
    "PreparedProviderSessionState",
    "ProviderSessionStateRequest",
    "prepare_provider_session_state",
]


def has_exact_transcript_match(
    *,
    worktree: Path,
    role: AgentRole,
    session_namespace: str,
    service: AgentService,
) -> bool:
    return plan_provider_session(
        ProviderSessionPlanRequest(
            worktree=worktree,
            role=role,
            namespace=session_namespace,
            service=service,
        )
    ).exact_transcript_match


__all__.append("has_exact_transcript_match")
