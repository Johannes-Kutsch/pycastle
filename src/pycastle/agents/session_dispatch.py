from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path

from .output_protocol import AgentRole
from ..session import RoleSession, RunKind
from ..session._provider_session_state import (
    PreparedProviderRunSession,
    PreparedProviderSessionState,
    ProviderSessionStateRequest,
    prepare_provider_session_state,
)
from ..session.run_session import LocalAuthSeedAction
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
    service_state_dir_relpath: str | None
    provider_state_dir_container_path: str | None
    success_recorder: Callable[[], None] = dataclasses.field(repr=False)
    on_provider_session_id: Callable[[str], None] = dataclasses.field(repr=False)
    prepare_for_run: Callable[[], None] = dataclasses.field(repr=False)
    _state: PreparedProviderSessionState = dataclasses.field(repr=False)
    auth_seed_action: LocalAuthSeedAction | None = None
    exact_transcript_match: bool = False

    @property
    def provider_state_dir_relpath(self) -> str | None:
        return self.service_state_dir_relpath

    def initial_provider_run_session(self) -> PreparedProviderRunSession:
        return self._state.initial_provider_run_session()

    def resumable_provider_run_session(self) -> PreparedProviderRunSession:
        return self._state.resumable_provider_run_session()


def prepare_agent_session(request: SessionDispatchRequest) -> PreparedAgentSession:
    session_state = prepare_provider_session_state(
        ProviderSessionStateRequest(
            worktree=request.mount_path,
            role=request.role,
            session_namespace=request.session_namespace,
            service=request.service,
        )
    )
    session_ref: dict[str, PreparedAgentSession] = {}

    def prepare_for_run() -> None:
        session_state.prepare_for_run()

    def on_provider_session_id(provider_session_id: str) -> None:
        prepared_session = session_ref["session"]
        prepared_session.provider_session_id = provider_session_id
        session_state.record_provider_session_id(provider_session_id)

    def success_recorder() -> None:
        session_state.record_successful_run()

    prepared_session = PreparedAgentSession(
        role_session=session_state.role_session,
        run_kind=session_state.run_kind,
        provider_session_id=session_state.provider_session_id,
        service_state_dir_relpath=session_state.service_state_dir_relpath,
        provider_state_dir_container_path=session_state.provider_state_dir_container_path(
            request.container_workspace
        ),
        success_recorder=success_recorder,
        on_provider_session_id=on_provider_session_id,
        prepare_for_run=prepare_for_run,
        auth_seed_action=session_state.auth_seed_action,
        exact_transcript_match=session_state.exact_transcript_match,
        _state=session_state,
    )
    session_ref["session"] = prepared_session
    return prepared_session


def record_successful_provider_session_metadata(
    prepared_session: PreparedAgentSession,
) -> None:
    prepared_session.success_recorder()


__all__ = [
    "PreparedAgentSession",
    "SessionDispatchRequest",
    "prepare_agent_session",
    "record_successful_provider_session_metadata",
]
