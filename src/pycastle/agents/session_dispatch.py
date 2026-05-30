from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Callable

from .output_protocol import AgentRole
from ..errors import HardAgentError
from ..session import RoleSession, RunKind
from ..session.run_session import LocalAuthSeedAction, RunSessionPlan
from ..services.agent_service import AgentService


@dataclasses.dataclass(frozen=True)
class SessionDispatchRequest:
    mount_path: Path
    role: AgentRole
    session_namespace: str
    service: AgentService


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
    _plan: RunSessionPlan = dataclasses.field(repr=False)
    auth_seed_action: LocalAuthSeedAction | None = None
    exact_transcript_match: bool = False

    @property
    def provider_state_dir_relpath(self) -> str | None:
        return self.service_state_dir_relpath

    def _preserved_codex_auth_bytes(self) -> bytes | None:
        auth_path = self._codex_auth_path()
        if auth_path is None or not auth_path.is_file():
            return None
        return auth_path.read_bytes()

    def _codex_auth_path(self) -> Path | None:
        service = getattr(self._plan, "service", None)
        if getattr(service, "name", None) != "codex":
            return None
        host_provider_state_dir = getattr(self._plan, "host_provider_state_dir", None)
        if host_provider_state_dir is None:
            return None
        return host_provider_state_dir / "auth.json"


def prepare_agent_session(request: SessionDispatchRequest) -> PreparedAgentSession:
    plan = RunSessionPlan.for_service(
        role=request.role,
        worktree=request.mount_path,
        namespace=request.session_namespace,
        service=request.service,
    )
    auth_seed_action = plan.auth_seed_action
    _require_dispatcher_auth_seed_source(auth_seed_action)
    role_session = RoleSession(
        request.mount_path,
        request.role,
        request.session_namespace,
    )
    provider_session_id = plan.prepared_provider_session_id()
    session_ref: dict[str, PreparedAgentSession] = {}

    def prepare_for_run() -> None:
        prepared_session = session_ref["session"]
        preserved_auth = prepared_session._preserved_codex_auth_bytes()
        if prepared_session.run_kind is RunKind.FRESH:
            prepared_session.role_session.start_fresh()
            if preserved_auth is not None:
                auth_path = prepared_session._codex_auth_path()
                if auth_path is not None:
                    auth_path.parent.mkdir(parents=True, exist_ok=True)
                    auth_path.write_bytes(preserved_auth)
        plan.prepare_host_provider_state_dir()

    def on_provider_session_id(provider_session_id: str) -> None:
        prepared_session = session_ref["session"]
        prepared_session.provider_session_id = provider_session_id
        plan.capture_provider_session_id(provider_session_id)

    def success_recorder() -> None:
        prepared_session = session_ref["session"]
        plan.record_successful_run(prepared_session.provider_session_id)

    prepared_session = PreparedAgentSession(
        role_session=role_session,
        run_kind=plan.run_kind,
        provider_session_id=provider_session_id,
        service_state_dir_relpath=plan.provider_state_dir_relpath,
        provider_state_dir_container_path=plan.provider_state_dir_container_path(
            "/home/agent/workspace"
        ),
        success_recorder=success_recorder,
        on_provider_session_id=on_provider_session_id,
        prepare_for_run=prepare_for_run,
        auth_seed_action=auth_seed_action,
        exact_transcript_match=plan.exact_transcript_match,
        _plan=plan,
    )
    session_ref["session"] = prepared_session
    return prepared_session


def _require_dispatcher_auth_seed_source(
    auth_seed_action: LocalAuthSeedAction | None,
) -> None:
    if auth_seed_action is None or auth_seed_action.source.exists():
        return
    raise HardAgentError(
        auth_seed_action.missing_source_message,
        status_code=401,
    )


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
