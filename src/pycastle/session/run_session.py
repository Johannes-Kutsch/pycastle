from __future__ import annotations

import dataclasses
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from ..agents.output_protocol import AgentRole
from .resume import RoleSession, RunKind

if TYPE_CHECKING:
    from ..services.agent_service import AgentService


class AuthSeedingRequirement(Enum):
    REQUIRED = "required"
    NOT_REQUIRED = "not_required"


class RecoveredSessionIdPersistence(Enum):
    PERSIST = "persist"
    SKIP = "skip"


def _codex_auth_seeding_requirement(
    state_dir: Path | None, run_kind: RunKind
) -> AuthSeedingRequirement:
    if state_dir is None:
        return AuthSeedingRequirement.NOT_REQUIRED
    if run_kind is RunKind.FRESH or not (state_dir / "auth.json").exists():
        return AuthSeedingRequirement.REQUIRED
    return AuthSeedingRequirement.NOT_REQUIRED


@dataclasses.dataclass(frozen=True)
class RunSessionPlan:
    role: AgentRole
    worktree: Path
    namespace: str
    service: AgentService
    run_kind: RunKind
    service_state_dir: Path | None
    provider_session_id: str | None
    auth_seeding_requirement: AuthSeedingRequirement
    recovered_session_id_persistence: RecoveredSessionIdPersistence
    exact_transcript_match: bool = False

    @classmethod
    def for_service(
        cls,
        *,
        role: AgentRole,
        worktree: Path,
        namespace: str,
        service: AgentService,
    ) -> RunSessionPlan:
        auth_seeding_requirement = AuthSeedingRequirement.NOT_REQUIRED
        recovered_session_id_persistence = RecoveredSessionIdPersistence.SKIP
        role_session = RoleSession(worktree, role, namespace)
        service_state = role_session.service_session_state(service)
        service_state_dir = service_state.state_dir
        handoff = role_session.exact_transcript_handoff_for_service(service)
        provider_identity = handoff.provider_identity
        provider_session_id = provider_identity.provider_session_id
        run_kind = provider_identity.run_kind
        if provider_identity.persist_provider_session_id:
            recovered_session_id_persistence = RecoveredSessionIdPersistence.PERSIST
        if service.name == "codex":
            auth_seeding_requirement = _codex_auth_seeding_requirement(
                service_state_dir, run_kind
            )
        exact_transcript_match = handoff.is_eligible
        return cls(
            role=role,
            worktree=worktree,
            namespace=namespace,
            service=service,
            run_kind=run_kind,
            service_state_dir=service_state_dir,
            provider_session_id=provider_session_id,
            auth_seeding_requirement=auth_seeding_requirement,
            recovered_session_id_persistence=recovered_session_id_persistence,
            exact_transcript_match=exact_transcript_match,
        )


__all__ = [
    "AuthSeedingRequirement",
    "RecoveredSessionIdPersistence",
    "RunSessionPlan",
]
