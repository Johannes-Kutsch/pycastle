from __future__ import annotations

import dataclasses
from pathlib import Path

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

    def start_fresh(self) -> None:
        preserved_auth = self._preserved_codex_auth_bytes()
        self.role_session.start_fresh()
        if preserved_auth is None:
            return
        auth_path = self._codex_auth_path()
        if auth_path is None:
            return
        auth_path.parent.mkdir(parents=True, exist_ok=True)
        auth_path.write_bytes(preserved_auth)

    def prepare_host_provider_state_dir(self) -> None:
        self._plan.prepare_host_provider_state_dir()

    def remember_provider_session_id(self, provider_session_id: str) -> None:
        self.provider_session_id = provider_session_id
        self._plan.capture_provider_session_id(provider_session_id)

    def record_successful_provider_session_metadata(self) -> None:
        self._plan.record_successful_run(self.provider_session_id)

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
    prepared_session.record_successful_provider_session_metadata()


__all__ = [
    "PreparedAgentSession",
    "SessionDispatchRequest",
    "prepare_agent_session",
    "record_successful_provider_session_metadata",
]
