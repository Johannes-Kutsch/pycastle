from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import TYPE_CHECKING
from collections.abc import Callable

from ..agents.output_protocol import AgentRole
from ..errors import HardAgentError
from .agent import plan_run_session
from .agent._planning import (
    AuthSeedingRequirement,
    LocalAuthSeedAction,
    RunSessionPlan,
    RunSessionPlanRequest,
)
from .resume import RoleSession, RunKind

if TYPE_CHECKING:
    from ..services.agent_service import AgentService


@dataclasses.dataclass(frozen=True)
class ProviderSessionStateRequest:
    worktree: Path
    role: AgentRole
    session_namespace: str
    service: AgentService


@dataclasses.dataclass
class PreparedProviderSessionState:
    role_session: RoleSession
    run_kind: RunKind
    provider_session_id: str | None
    service_state_dir_relpath: str | None
    service_state_dir_path: Path | None
    auth_seeding_requirement: AuthSeedingRequirement
    _plan: RunSessionPlan = dataclasses.field(repr=False)
    auth_seed_action: LocalAuthSeedAction | None = None
    exact_transcript_match: bool = False

    @property
    def provider_state_dir_relpath(self) -> str | None:
        return self.service_state_dir_relpath

    def provider_state_dir_container_path(self, container_workspace: str) -> str | None:
        return self._plan.provider_state_dir_container_path(container_workspace)

    def initial_provider_run_session(self) -> PreparedProviderRunSession:
        return PreparedProviderRunSession(
            run_kind=self.run_kind,
            provider_session_id=self.provider_session_id,
            _provider_session_id_recorder=self.record_provider_session_id,
            _success_recorder=self.record_successful_run,
        )

    def resumable_provider_run_session(self) -> PreparedProviderRunSession:
        run_kind = RunKind.RESUME
        if self.provider_session_id is None:
            run_kind = RunKind.FRESH
        return PreparedProviderRunSession(
            run_kind=run_kind,
            provider_session_id=self.provider_session_id,
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
        service = getattr(self._plan, "service", None)
        if getattr(service, "name", None) != "codex":
            return None
        host_provider_state_dir = getattr(self._plan, "host_provider_state_dir", None)
        if host_provider_state_dir is None:
            return None
        return host_provider_state_dir / "auth.json"


@dataclasses.dataclass(frozen=True)
class PreparedProviderRunSession:
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


def prepare_provider_session_state(
    request: ProviderSessionStateRequest,
) -> PreparedProviderSessionState:
    plan = plan_run_session(
        RunSessionPlanRequest(
            role=request.role,
            worktree=request.worktree,
            namespace=request.session_namespace,
            service=request.service,
        )
    )
    auth_seed_action = plan.auth_seed_action
    role_session = RoleSession(
        request.worktree,
        request.role,
        request.session_namespace,
    )
    provider_session_id = plan.prepared_provider_session_id()
    return PreparedProviderSessionState(
        role_session=role_session,
        run_kind=plan.run_kind,
        provider_session_id=provider_session_id,
        service_state_dir_relpath=plan.provider_state_dir_relpath,
        service_state_dir_path=plan.host_provider_state_dir,
        auth_seeding_requirement=plan.auth_seeding_requirement,
        _plan=plan,
        auth_seed_action=auth_seed_action,
        exact_transcript_match=plan.exact_transcript_match,
    )


def recover_codex_rollout_thread_id(state_dir: Path) -> str | None:
    sessions_dir = state_dir / "sessions"
    if not sessions_dir.is_dir():
        return None

    found: set[str] = set()
    for rollout in sessions_dir.rglob("rollout-*.jsonl"):
        try:
            lines = rollout.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") != "thread.started":
                continue
            thread_id = obj.get("thread_id")
            if isinstance(thread_id, str) and thread_id.strip():
                found.add(thread_id.strip())

    return next(iter(found)) if len(found) == 1 else None


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
    "PreparedProviderRunSession",
    "PreparedProviderSessionState",
    "ProviderSessionStateRequest",
    "prepare_provider_session_state",
    "recover_codex_rollout_thread_id",
]
