from __future__ import annotations

import dataclasses
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from ..agents.output_protocol import AgentRole

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
        provider_session_id: str | None,
        auth_seeding_requirement: AuthSeedingRequirement,
        recovered_session_id_persistence: RecoveredSessionIdPersistence,
    ) -> RunSessionPlan:
        state_dir_relpath = service.state_dir_relpath(role, namespace)
        service_state_dir = (
            worktree / state_dir_relpath if state_dir_relpath is not None else None
        )
        return cls(
            role=role,
            worktree=worktree,
            namespace=namespace,
            service=service,
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
