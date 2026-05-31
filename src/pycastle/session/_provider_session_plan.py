from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

from ..agents.output_protocol import AgentRole
from ._provider_session_decision import (
    AuthSeedingRequirement,
    LocalAuthSeedAction,
    ProviderSessionDecision,
    RecoveredSessionIdPersistence,
)
from .provider_session_state import (
    clear_service_session_metadata,
    save_service_session_metadata,
)
from .resume import (
    RoleSession,
    RunKind,
    _provider_identity_from_session_state,
    _normalize_state_dir_relpath,
)
from ..services.provider_session_state import (
    ProviderSessionStateRequest as ServiceProviderSessionStateRequest,
)

if TYPE_CHECKING:
    from ..services.agent_service import AgentService


@dataclasses.dataclass(frozen=True)
class ProviderRunStatePlanRequest:
    worktree: Path
    role: AgentRole
    namespace: str
    service: AgentService


@dataclasses.dataclass
class ProviderRunStatePlan:
    role_session: RoleSession = dataclasses.field(repr=False, compare=False)
    service_name: str
    run_kind: RunKind
    provider_state_dir: Path | None
    provider_state_dir_relpath: str | None
    provider_session_id: str | None
    requires_host_codex_auth: bool
    recovered_session_id_persistence: RecoveredSessionIdPersistence
    service_state_dir: Path | None = None
    exact_transcript_match: bool = False
    auth_seed_action: LocalAuthSeedAction | None = None

    @property
    def auth_seeding_requirement(self) -> AuthSeedingRequirement:
        if self.requires_host_codex_auth:
            return AuthSeedingRequirement.REQUIRED
        return AuthSeedingRequirement.NOT_REQUIRED

    def provider_session_decision(self) -> ProviderSessionDecision:
        return ProviderSessionDecision(
            run_kind=self.run_kind,
            provider_session_id=self.provider_session_id,
            state_dir_relpath=self.provider_state_dir_relpath,
            state_dir_path=self.provider_state_dir,
            recovered_session_id_persistence=self.recovered_session_id_persistence,
            service_state_dir=self.service_state_dir,
            exact_transcript_match=self.exact_transcript_match,
            auth_seeding_requirement=self.auth_seeding_requirement,
            auth_seed_action=self.auth_seed_action,
        )

    def provider_state_dir_container_path(
        self,
        *,
        worktree: Path,
        container_workspace: str,
    ) -> str | None:
        return self.provider_session_decision().container_state_dir_path(
            worktree=worktree,
            service_name=self.service_name,
            container_workspace=container_workspace,
        )

    def prepare_provider_state_dir(self) -> None:
        if self.provider_state_dir is not None:
            self.provider_state_dir.mkdir(parents=True, exist_ok=True)
        if self.auth_seed_action is not None:
            self.auth_seed_action.apply()

    def prepared_provider_session_id(self) -> str | None:
        provider_session_id = self.provider_session_id
        if provider_session_id is None:
            return None
        if (
            self.recovered_session_id_persistence
            is RecoveredSessionIdPersistence.PERSIST
        ):
            self.remember_provider_session_id(provider_session_id)
        return provider_session_id

    def remember_provider_session_id(self, provider_session_id: str) -> None:
        self.provider_session_id = provider_session_id
        if self.service_name == "opencode" and self.service_state_dir is not None:
            session_id_path = self.service_state_dir / "session_id"
            session_id_path.parent.mkdir(parents=True, exist_ok=True)
            session_id_path.write_text(provider_session_id, encoding="utf-8")
        if self.service_name not in {"codex", "opencode"}:
            return
        self.role_session.save_service_session_id(
            self.service_name,
            provider_session_id,
        )


ProviderSessionPlanRequest = ProviderRunStatePlanRequest


def record_observed_provider_session_id(
    *,
    worktree: Path,
    role: AgentRole,
    namespace: str,
    service_name: str,
    service_state_dir: Path | None,
    provider_session_id: str,
) -> None:
    plan = ProviderRunStatePlan(
        role_session=RoleSession(worktree, role, namespace),
        service_name=service_name,
        run_kind=RunKind.FRESH,
        provider_state_dir=service_state_dir,
        provider_state_dir_relpath=None,
        provider_session_id=None,
        requires_host_codex_auth=False,
        recovered_session_id_persistence=RecoveredSessionIdPersistence.SKIP,
        service_state_dir=service_state_dir,
    )
    plan.remember_provider_session_id(provider_session_id)


def capture_provider_session_id(
    *,
    worktree: Path,
    role: AgentRole,
    namespace: str,
    service_name: str,
    service_state_dir: Path | None,
    provider_session_id: str,
) -> None:
    record_observed_provider_session_id(
        worktree=worktree,
        role=role,
        namespace=namespace,
        service_name=service_name,
        service_state_dir=service_state_dir,
        provider_session_id=provider_session_id,
    )


def record_successful_provider_session_metadata(
    *,
    worktree: Path,
    role: AgentRole,
    namespace: str,
    service_name: str,
    provider_session_id: str | None,
) -> None:
    role_session_path = RoleSession(worktree, role, namespace).path
    if provider_session_id is None:
        clear_service_session_metadata(role_session_path, service_name)
        return
    save_service_session_metadata(
        role_session_path,
        service_name,
        provider_session_id,
    )


def plan_provider_session(
    request: ProviderRunStatePlanRequest,
) -> ProviderSessionDecision:
    return plan_provider_run_state(request).provider_session_decision()


def plan_provider_run_state(
    request: ProviderRunStatePlanRequest,
) -> ProviderRunStatePlan:
    role_session = RoleSession(request.worktree, request.role, request.namespace)
    service_state = role_session.service_session_state(request.service)

    raw_state_dir_relpath = request.service.state_dir_relpath(
        request.role,
        request.namespace,
    )
    state_dir_relpath = _normalize_state_dir_relpath(
        request.role,
        request.namespace,
        request.service.name,
        raw_state_dir_relpath,
    )
    host_state_dir = service_state.state_dir
    if state_dir_relpath is not None and state_dir_relpath != raw_state_dir_relpath:
        host_state_dir = request.worktree / state_dir_relpath.rstrip("/")

    provider_session_state = request.service.provider_session_state(
        ServiceProviderSessionStateRequest(
            role_session=role_session,
            provider_state_dir=host_state_dir,
            has_resumable_provider_state=service_state.has_resumable_provider_state,
            state_dir_relpath=state_dir_relpath,
            require_exact_transcript_match=True,
            preferred_provider_session_id=(
                role_session.session_uuid()
                if request.service.name == "claude"
                else None
            ),
        )
    )
    provider_identity = _provider_identity_from_session_state(
        provider_session_state,
        has_resumable_provider_state=service_state.has_resumable_provider_state,
    )
    recovered_session_id_persistence = RecoveredSessionIdPersistence.SKIP
    if provider_identity.persist_provider_session_id:
        recovered_session_id_persistence = RecoveredSessionIdPersistence.PERSIST
    auth_seeding_requirement = (
        provider_session_state.auth_seeding_requirement
        or AuthSeedingRequirement.NOT_REQUIRED
    )
    return ProviderRunStatePlan(
        role_session=role_session,
        service_name=request.service.name,
        run_kind=provider_identity.run_kind,
        provider_session_id=provider_identity.provider_session_id,
        provider_state_dir=provider_session_state.state_dir_path or host_state_dir,
        provider_state_dir_relpath=(
            provider_session_state.state_dir_relpath or state_dir_relpath
        ),
        requires_host_codex_auth=(
            auth_seeding_requirement is AuthSeedingRequirement.REQUIRED
        ),
        recovered_session_id_persistence=recovered_session_id_persistence,
        service_state_dir=service_state.state_dir,
        exact_transcript_match=provider_session_state.exact_transcript_match,
        auth_seed_action=provider_session_state.auth_seed_action,
    )


__all__ = [
    "AuthSeedingRequirement",
    "capture_provider_session_id",
    "LocalAuthSeedAction",
    "ProviderRunStatePlan",
    "ProviderRunStatePlanRequest",
    "ProviderSessionDecision",
    "ProviderSessionPlanRequest",
    "plan_provider_run_state",
    "plan_provider_session",
]
