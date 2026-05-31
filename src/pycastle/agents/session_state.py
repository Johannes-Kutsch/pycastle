from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path

from .output_protocol import AgentRole
from ..errors import HardAgentError
from ..session import RoleSession, RunKind
from ..session.agent import LocalAuthSeedAction, RunSessionPlan, RunSessionPlanRequest
from ..session.agent import plan_run_session as _plan_run_session
from ..services.agent_service import AgentService
from ..services.provider_session_state import (
    ProviderSessionState,
    ProviderSessionStateRequest as ServiceProviderSessionStateRequest,
)


@dataclasses.dataclass(frozen=True)
class AgentRunSessionStateRequest:
    worktree: Path
    role: AgentRole
    session_namespace: str
    service: AgentService
    run_session_plan: RunSessionPlan | None = None


@dataclasses.dataclass
class AgentRunSessionState:
    role_session: RoleSession
    run_kind: RunKind
    provider_session_id: str | None
    service_state_dir_relpath: str | None
    service_state_dir_path: Path | None
    _plan: RunSessionPlan = dataclasses.field(repr=False)
    auth_seed_action: LocalAuthSeedAction | None = None
    exact_transcript_match: bool = False

    @property
    def provider_state_dir_relpath(self) -> str | None:
        return self.service_state_dir_relpath

    @property
    def codex_auth_seed_input(self) -> Path | None:
        if self.auth_seed_action is None:
            return None
        return self.auth_seed_action.source

    def provider_state_dir_container_path(self, container_workspace: str) -> str | None:
        return self._plan.provider_state_dir_container_path(container_workspace)

    def initial_provider_run_session(self) -> PreparedAgentProviderRunSession:
        return PreparedAgentProviderRunSession(
            run_kind=self.run_kind,
            provider_session_id=self.provider_session_id,
            _provider_session_id_recorder=self.record_provider_session_id,
            _success_recorder=self.record_successful_run,
        )

    def resumable_provider_run_session(self) -> PreparedAgentProviderRunSession:
        provider_session_state = self._resume_provider_session_state()
        return PreparedAgentProviderRunSession(
            run_kind=provider_session_state.run_kind,
            provider_session_id=provider_session_state.provider_session_id,
            _provider_session_id_recorder=self.record_provider_session_id,
            _success_recorder=self.record_successful_run,
        )

    def protocol_reprompt_provider_run_session(
        self,
    ) -> PreparedAgentProviderRunSession | None:
        provider_session_state = self._resume_provider_session_state()
        if not provider_session_state.allow_protocol_reprompt:
            return None
        return PreparedAgentProviderRunSession(
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
        self._plan.prepare_host_provider_state_dir()

    def record_provider_session_id(self, provider_session_id: str) -> None:
        self.provider_session_id = provider_session_id
        self._plan.capture_provider_session_id(provider_session_id)

    def record_successful_run(self) -> None:
        self._plan.record_successful_run(self.provider_session_id)

    def _preserved_codex_auth_bytes(self) -> bytes | None:
        auth_path = self._codex_auth_path()
        if auth_path is None or not auth_path.is_file():
            return None
        return auth_path.read_bytes()

    def _codex_auth_path(self) -> Path | None:
        if self._plan.service.name != "codex":
            return None
        host_provider_state_dir = self._plan.host_provider_state_dir
        if host_provider_state_dir is None:
            return None
        return host_provider_state_dir / "auth.json"

    def _resume_provider_session_state(self):
        if self.provider_session_id is not None:
            return ProviderSessionState(
                run_kind=RunKind.RESUME,
                provider_session_id=self.provider_session_id,
                state_dir_relpath=self.provider_state_dir_relpath,
                state_dir_path=self.service_state_dir_path,
                exact_transcript_match=self.exact_transcript_match,
            )
        service_state = self.role_session.service_session_state(self._plan.service)
        return self._plan.service.provider_session_state(
            ServiceProviderSessionStateRequest(
                role_session=self.role_session,
                provider_state_dir=service_state.state_dir,
                has_resumable_provider_state=service_state.has_resumable_provider_state,
                state_dir_relpath=service_state.state_dir_relpath,
                preferred_provider_session_id=self.provider_session_id,
                force_resume=True,
            )
        )


@dataclasses.dataclass(frozen=True)
class PreparedAgentProviderRunSession:
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


def prepare_agent_run_session_state(
    request: AgentRunSessionStateRequest,
) -> AgentRunSessionState:
    plan = request.run_session_plan or _plan_run_session(
        RunSessionPlanRequest(
            role=request.role,
            worktree=request.worktree,
            namespace=request.session_namespace,
            service=request.service,
        )
    )
    auth_seed_action = plan.auth_seed_action
    if auth_seed_action is not None:
        auth_seed_action.require_source()
    role_session = RoleSession(
        request.worktree,
        request.role,
        request.session_namespace,
    )
    provider_session_id = plan.prepared_provider_session_id()
    return AgentRunSessionState(
        role_session=role_session,
        run_kind=plan.run_kind,
        provider_session_id=provider_session_id,
        service_state_dir_relpath=plan.provider_state_dir_relpath,
        service_state_dir_path=plan.host_provider_state_dir,
        auth_seed_action=auth_seed_action,
        exact_transcript_match=plan.exact_transcript_match,
        _plan=plan,
    )


def record_observed_provider_session_id(
    session_state: AgentRunSessionState,
    provider_session_id: str,
) -> None:
    session_state.record_provider_session_id(provider_session_id)


def record_successful_provider_session_metadata(
    session_state: AgentRunSessionState,
) -> None:
    session_state.record_successful_run()


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
    "AgentRunSessionState",
    "AgentRunSessionStateRequest",
    "PreparedAgentProviderRunSession",
    "prepare_agent_run_session_state",
    "record_observed_provider_session_id",
    "record_successful_provider_session_metadata",
]
