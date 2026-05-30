from __future__ import annotations

import dataclasses
from pathlib import Path

from .output_protocol import AgentRole
from ..session import RoleSession, RunKind
from ..session.run_session import LocalAuthSeedAction, RunSessionPlan
from ..services.agent_service import AgentService


@dataclasses.dataclass(frozen=True)
class SessionDispatchRequest:
    mount_path: Path
    role: AgentRole
    session_namespace: str
    service: AgentService
    container_workspace: str


@dataclasses.dataclass
class PreparedAgentSession:
    role_session: RoleSession
    run_kind: RunKind
    provider_session_id: str | None
    provider_state_dir_relpath: str | None
    provider_state_dir_container_path: str | None
    _plan: RunSessionPlan = dataclasses.field(repr=False)
    auth_seed_action: LocalAuthSeedAction | None = None
    exact_transcript_match: bool = False

    def prepare_host_provider_state_dir(self) -> None:
        self._plan.prepare_host_provider_state_dir()

    def remember_provider_session_id(self, provider_session_id: str) -> None:
        self.provider_session_id = provider_session_id
        self._plan.capture_provider_session_id(provider_session_id)


def prepare_agent_session(request: SessionDispatchRequest) -> PreparedAgentSession:
    role_session = RoleSession(
        request.mount_path,
        request.role,
        request.session_namespace,
    )
    plan = RunSessionPlan.for_service(
        role=request.role,
        worktree=request.mount_path,
        namespace=request.session_namespace,
        service=request.service,
    )
    auth_seed_action = plan.auth_seed_action
    if auth_seed_action is not None:
        auth_seed_action.require_source()
    return PreparedAgentSession(
        role_session=role_session,
        run_kind=plan.run_kind,
        provider_session_id=plan.prepared_provider_session_id(),
        provider_state_dir_relpath=plan.provider_state_dir_relpath,
        provider_state_dir_container_path=plan.provider_state_dir_container_path(
            request.container_workspace
        ),
        auth_seed_action=auth_seed_action,
        exact_transcript_match=plan.exact_transcript_match,
        _plan=plan,
    )


def record_successful_provider_session_metadata(
    prepared_session: PreparedAgentSession,
) -> None:
    prepared_session._plan.record_successful_run(prepared_session.provider_session_id)


__all__ = [
    "PreparedAgentSession",
    "SessionDispatchRequest",
    "prepare_agent_session",
    "record_successful_provider_session_metadata",
]
