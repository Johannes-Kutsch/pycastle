from __future__ import annotations

import dataclasses
from pathlib import Path

from .output_protocol import AgentRole
from ..services.agent_service import AgentService
from ..session.agent import LocalAuthSeedAction, RunSessionPlan
from ..session.resume import RoleSession, RunKind
from ..session.run_dispatch import (
    PreparedRunSession,
    RunSessionRequest,
    prepare_run_session,
    record_successful_provider_session_metadata,
)


@dataclasses.dataclass(frozen=True)
class SessionDispatchRequest:
    mount_path: Path
    role: AgentRole
    session_namespace: str
    service: AgentService
    container_workspace: str
    run_session_plan: RunSessionPlan | None = None


PreparedAgentSession = PreparedRunSession


def prepare_agent_session(request: SessionDispatchRequest) -> PreparedAgentSession:
    return prepare_run_session(
        RunSessionRequest(
            worktree=request.mount_path,
            role=request.role,
            session_namespace=request.session_namespace,
            service=request.service,
            container_workspace=request.container_workspace,
            run_session_plan=request.run_session_plan,
        )
    )


__all__ = [
    "LocalAuthSeedAction",
    "PreparedAgentSession",
    "RoleSession",
    "RunKind",
    "SessionDispatchRequest",
    "prepare_agent_session",
    "record_successful_provider_session_metadata",
]
