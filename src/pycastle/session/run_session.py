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

    @classmethod
    def for_service(
        cls,
        *,
        role: AgentRole,
        worktree: Path,
        namespace: str,
        service: AgentService,
        provider_session_id: str | None = None,
        auth_seeding_requirement: AuthSeedingRequirement = (
            AuthSeedingRequirement.NOT_REQUIRED
        ),
        recovered_session_id_persistence: RecoveredSessionIdPersistence = (
            RecoveredSessionIdPersistence.SKIP
        ),
    ) -> RunSessionPlan:
        state_dir_relpath = service.state_dir_relpath(role, namespace)
        service_state_dir = (
            worktree / state_dir_relpath if state_dir_relpath is not None else None
        )
        run_kind = (
            RunKind.RESUME
            if service_state_dir is not None and service.is_resumable(service_state_dir)
            else RunKind.FRESH
        )
        if provider_session_id is None and service.name == "claude":
            provider_session_id = RoleSession(worktree, role, namespace).session_uuid()
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
        )


__all__ = [
    "AuthSeedingRequirement",
    "RecoveredSessionIdPersistence",
    "RunSessionPlan",
]
