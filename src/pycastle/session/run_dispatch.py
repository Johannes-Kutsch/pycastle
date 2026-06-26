from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import cast

from pycastle.runtime_session import (
    ProviderSessionState,
    ProviderSessionStateRequest,
    RunKind,
)
from pycastle.session_planning import (
    ProviderRunStatePlanRequest,
    plan_provider_run_state,
)

from ..agents.output_protocol import AgentRole
from ..provider_session_adapter import provider_session_adapter_for_service
from ..services.runtime_services import AgentService
from .agent import LocalAuthSeedAction, RunSessionPlan, RunSessionPlanRequest
from .agent._planning import plan_run_session
from .role import RoleSession
from .service_session_store import (
    store_for_role_session,
)


@dataclasses.dataclass(frozen=True)
class AgentRunSessionStateRequest:
    worktree: Path
    role: AgentRole
    session_namespace: str
    service: AgentService
    run_session_plan: RunSessionPlan | None = None
    require_exact_transcript_for_strict_resume: bool = False


@dataclasses.dataclass
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
    require_exact_transcript_for_strict_resume: bool = False
    _observed_provider_session_id: bool = dataclasses.field(
        default=False,
        repr=False,
    )

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
        if self.auth_seed_action is not None:
            self.auth_seed_action.require_source()
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
        self._observed_provider_session_id = True
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

    def _resume_provider_session_state(self) -> ProviderSessionState:
        if self.provider_session_id is not None and (
            self._observed_provider_session_id
            or self.exact_transcript_match
            or not self.require_exact_transcript_for_strict_resume
        ):
            return ProviderSessionState(
                run_kind=RunKind.RESUME,
                provider_session_id=self.provider_session_id,
                state_dir_relpath=self.provider_state_dir_relpath,
                state_dir_path=self.service_state_dir_path,
                exact_transcript_match=self.exact_transcript_match,
            )
        if (
            self.require_exact_transcript_for_strict_resume
            and self.provider_session_id is not None
            and not self.exact_transcript_match
        ):
            return ProviderSessionState(
                run_kind=RunKind.FRESH,
                provider_session_id=None,
                state_dir_relpath=self.provider_state_dir_relpath,
                state_dir_path=self.service_state_dir_path,
                allow_protocol_reprompt=False,
            )
        return self._plan.service.provider_session_state(
            ProviderSessionStateRequest(
                role_session=store_for_role_session(self.role_session),
                provider_state_dir=self.service_state_dir_path,
                has_resumable_provider_state=(
                    self.service_state_dir_path is not None
                    and self._plan.service.is_resumable(self.service_state_dir_path)
                ),
                state_dir_relpath=self.provider_state_dir_relpath,
                preferred_provider_session_id=self.provider_session_id,
                force_resume=True,
            )
        )


@dataclasses.dataclass(frozen=True)
class RunSessionRequest:
    worktree: Path
    role: AgentRole
    session_namespace: str
    service: AgentService
    container_workspace: str
    run_session_plan: RunSessionPlan | None = None
    require_exact_transcript_for_strict_resume: bool = False


@dataclasses.dataclass
class PreparedRunSession:
    role_session: RoleSession
    run_kind: RunKind
    provider_session_id: str | None
    service_state_dir_relpath: str | None
    provider_state_dir_container_path: str | None
    success_recorder: Callable[[], None] = dataclasses.field(repr=False)
    on_provider_session_id: Callable[[str], None] = dataclasses.field(repr=False)
    prepare_for_run: Callable[[], None] = dataclasses.field(repr=False)
    _state: AgentRunSessionState = dataclasses.field(repr=False)
    auth_seed_action: LocalAuthSeedAction | None = None
    exact_transcript_match: bool = False

    @property
    def provider_state_dir_relpath(self) -> str | None:
        return self.service_state_dir_relpath

    def initial_provider_run_session(self) -> PreparedAgentProviderRunSession:
        state_run_session = self._state.initial_provider_run_session()
        return PreparedAgentProviderRunSession(
            run_kind=state_run_session.run_kind,
            provider_session_id=state_run_session.provider_session_id,
            _provider_session_id_recorder=self.on_provider_session_id,
            _success_recorder=self.success_recorder,
        )

    def resumable_provider_run_session(self) -> PreparedAgentProviderRunSession:
        state_run_session = self._state.resumable_provider_run_session()
        return PreparedAgentProviderRunSession(
            run_kind=state_run_session.run_kind,
            provider_session_id=state_run_session.provider_session_id,
            _provider_session_id_recorder=self.on_provider_session_id,
            _success_recorder=self.success_recorder,
        )

    def protocol_reprompt_provider_run_session(
        self,
    ) -> PreparedAgentProviderRunSession | None:
        state_run_session = self._state.protocol_reprompt_provider_run_session()
        if state_run_session is None:
            return None
        return PreparedAgentProviderRunSession(
            run_kind=state_run_session.run_kind,
            provider_session_id=state_run_session.provider_session_id,
            _provider_session_id_recorder=self.on_provider_session_id,
            _success_recorder=self.success_recorder,
        )


def prepare_agent_run_session_state(
    request: AgentRunSessionStateRequest,
) -> AgentRunSessionState:
    plan = request.run_session_plan or plan_run_session(
        RunSessionPlanRequest(
            role=request.role,
            worktree=request.worktree,
            namespace=request.session_namespace,
            service=request.service,
        )
    )
    auth_seed_action = cast(LocalAuthSeedAction | None, plan.auth_seed_action)
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
        require_exact_transcript_for_strict_resume=(
            request.require_exact_transcript_for_strict_resume
        ),
        _plan=plan,
    )


def record_observed_provider_session_id(
    session_state: AgentRunSessionState,
    provider_session_id: str,
) -> None:
    session_state.record_provider_session_id(provider_session_id)


def record_successful_provider_session_metadata(
    prepared_session: PreparedRunSession,
) -> None:
    prepared_session.success_recorder()


def has_exact_transcript_match(
    *,
    worktree: Path,
    role: AgentRole,
    session_namespace: str,
    service: AgentService,
) -> bool:
    return plan_provider_run_state(
        ProviderRunStatePlanRequest(
            worktree=worktree,
            role=role,
            namespace=session_namespace,
            service=service,
            role_session=store_for_role_session(
                RoleSession(worktree, role, session_namespace)
            ),
            provider_session_adapter=provider_session_adapter_for_service(service),
        )
    ).exact_transcript_match


def prepare_run_session(request: RunSessionRequest) -> PreparedRunSession:
    session_state = prepare_agent_run_session_state(
        AgentRunSessionStateRequest(
            worktree=request.worktree,
            role=request.role,
            session_namespace=request.session_namespace,
            service=request.service,
            run_session_plan=request.run_session_plan,
            require_exact_transcript_for_strict_resume=(
                request.require_exact_transcript_for_strict_resume
            ),
        )
    )
    session_ref: dict[str, PreparedRunSession] = {}

    def prepare_for_run() -> None:
        session_state.prepare_for_run()

    def on_provider_session_id(provider_session_id: str) -> None:
        prepared_session = session_ref["session"]
        prepared_session.provider_session_id = provider_session_id
        record_observed_provider_session_id(session_state, provider_session_id)

    def success_recorder() -> None:
        session_state.record_successful_run()

    prepared_session = PreparedRunSession(
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


__all__ = [
    "AgentRunSessionState",
    "AgentRunSessionStateRequest",
    "PreparedAgentProviderRunSession",
    "PreparedRunSession",
    "RunSessionRequest",
    "has_exact_transcript_match",
    "prepare_agent_run_session_state",
    "prepare_run_session",
    "record_observed_provider_session_id",
    "record_successful_provider_session_metadata",
]
