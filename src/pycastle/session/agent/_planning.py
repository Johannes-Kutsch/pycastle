from __future__ import annotations

import dataclasses
import shutil
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from ...agents.output_protocol import AgentRole
from ...errors import HardAgentError
from .._provider_session_plan import (
    ProviderSessionPlan,
    ProviderSessionPlanRequest,
    _preserves_role_provider_layout,
    plan_provider_session,
)
from ..resume import (
    RoleSession,
    RunKind,
)

if TYPE_CHECKING:
    from ...services.agent_service import AgentService


class AuthSeedingRequirement(Enum):
    REQUIRED = "required"
    NOT_REQUIRED = "not_required"


class RecoveredSessionIdPersistence(Enum):
    PERSIST = "persist"
    SKIP = "skip"


@dataclasses.dataclass(frozen=True)
class LocalAuthSeedAction:
    source: Path
    destination: Path
    missing_source_message: str = dataclasses.field(
        default="Codex authentication missing: run `codex login` on the host.",
        compare=False,
    )

    def require_source(self) -> Path:
        if not self.source.exists():
            raise HardAgentError(
                self.missing_source_message,
                status_code=401,
            )
        return self.source

    def apply(self) -> None:
        if self.destination.exists():
            return
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.source, self.destination)


def _persist_provider_state_session_id(
    service_name: str,
    state_dir: Path | None,
    provider_session_id: str,
) -> None:
    if state_dir is None or service_name != "opencode":
        return
    session_id_path = state_dir / "session_id"
    session_id_path.parent.mkdir(parents=True, exist_ok=True)
    session_id_path.write_text(provider_session_id, encoding="utf-8")


@dataclasses.dataclass(frozen=True)
class RunSessionPlanRequest:
    role: AgentRole
    worktree: Path
    namespace: str
    service: AgentService


@dataclasses.dataclass(frozen=True)
class RunSessionPlan:
    role: AgentRole
    worktree: Path
    namespace: str
    service: AgentService
    run_kind: RunKind
    service_state_dir: Path | None
    provider_state_dir_relpath: str | None
    host_provider_state_dir: Path | None
    provider_session_id: str | None
    auth_seeding_requirement: AuthSeedingRequirement
    recovered_session_id_persistence: RecoveredSessionIdPersistence
    provider_session_plan: ProviderSessionPlan | None = None
    auth_seed_action: LocalAuthSeedAction | None = None
    exact_transcript_match: bool = False

    def __post_init__(self) -> None:
        if self.provider_session_plan is not None:
            return
        object.__setattr__(
            self,
            "provider_session_plan",
            ProviderSessionPlan(
                state_dir_relpath=self.provider_state_dir_relpath,
                host_state_dir=self.host_provider_state_dir,
                run_kind=self.run_kind,
                provider_session_id=self.provider_session_id,
            ),
        )

    def provider_state_dir_container_path(self, container_workspace: str) -> str | None:
        if (
            self.service.name == "opencode"
            and self.run_kind is RunKind.RESUME
            and self.service_state_dir is not None
        ):
            try:
                service_relpath = self.service_state_dir.relative_to(self.worktree)
            except ValueError:
                pass
            else:
                return f"{container_workspace}/{service_relpath.as_posix()}/"
        if self.host_provider_state_dir is not None:
            try:
                host_relpath = self.host_provider_state_dir.relative_to(self.worktree)
            except ValueError:
                pass
            else:
                return f"{container_workspace}/{host_relpath.as_posix()}/"
        if self.provider_state_dir_relpath is None:
            return None
        return f"{container_workspace}/{self.provider_state_dir_relpath}"

    def prepared_provider_session_id(self) -> str | None:
        provider_session_id = self.provider_session_id
        if provider_session_id is None:
            return None
        if (
            self.recovered_session_id_persistence
            is RecoveredSessionIdPersistence.PERSIST
        ):
            self.capture_provider_session_id(provider_session_id)
        return provider_session_id

    def prepare_host_provider_state_dir(self) -> None:
        if self.host_provider_state_dir is None:
            return
        self.host_provider_state_dir.mkdir(parents=True, exist_ok=True)
        if self.auth_seed_action is not None:
            self.auth_seed_action.apply()

    def capture_provider_session_id(self, provider_session_id: str) -> None:
        object.__setattr__(self, "provider_session_id", provider_session_id)
        _persist_provider_state_session_id(
            self.service.name,
            self.service_state_dir,
            provider_session_id,
        )
        if not _preserves_role_provider_layout(self.service.name):
            return
        RoleSession(
            self.worktree,
            self.role,
            self.namespace,
        ).save_service_session_id(self.service.name, provider_session_id)

    def record_successful_run(self, provider_session_id: str | None = None) -> None:
        session_id = provider_session_id or self.provider_session_id
        if session_id is None:
            return
        RoleSession(
            self.worktree,
            self.role,
            self.namespace,
        ).save_service_session_metadata(self.service.name, session_id)

    @classmethod
    def for_service(
        cls,
        *,
        role: AgentRole,
        worktree: Path,
        namespace: str,
        service: AgentService,
    ) -> RunSessionPlan:
        return plan_run_session(
            RunSessionPlanRequest(
                role=role,
                worktree=worktree,
                namespace=namespace,
                service=service,
            )
        )


def plan_run_session(request: RunSessionPlanRequest) -> RunSessionPlan:
    auth_seeding_requirement = AuthSeedingRequirement.NOT_REQUIRED
    recovered_session_id_persistence = RecoveredSessionIdPersistence.SKIP
    auth_seed_action: LocalAuthSeedAction | None = None
    provider_session = plan_provider_session(
        ProviderSessionPlanRequest(
            worktree=request.worktree,
            role=request.role,
            namespace=request.namespace,
            service=request.service,
        )
    )
    provider_plan = provider_session.plan
    if provider_session.persist_provider_session_id:
        recovered_session_id_persistence = RecoveredSessionIdPersistence.PERSIST
    if provider_plan.requires_codex_auth_seed:
        auth_seeding_requirement = AuthSeedingRequirement.REQUIRED
        if provider_plan.host_state_dir is not None:
            auth_seed_action = LocalAuthSeedAction(
                source=Path.home() / ".codex" / "auth.json",
                destination=provider_plan.host_state_dir / "auth.json",
            )
    return RunSessionPlan(
        role=request.role,
        worktree=request.worktree,
        namespace=request.namespace,
        service=request.service,
        provider_session_plan=provider_plan,
        run_kind=provider_plan.run_kind,
        service_state_dir=provider_session.service_state_dir,
        provider_state_dir_relpath=provider_plan.state_dir_relpath,
        host_provider_state_dir=provider_plan.host_state_dir,
        provider_session_id=provider_plan.provider_session_id,
        auth_seeding_requirement=auth_seeding_requirement,
        recovered_session_id_persistence=recovered_session_id_persistence,
        auth_seed_action=auth_seed_action,
        exact_transcript_match=provider_session.exact_transcript_match,
    )


__all__ = [
    "AuthSeedingRequirement",
    "LocalAuthSeedAction",
    "RecoveredSessionIdPersistence",
    "RunSessionPlan",
    "RunSessionPlanRequest",
    "plan_run_session",
]
