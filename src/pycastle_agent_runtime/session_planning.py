from __future__ import annotations

import dataclasses
import shutil
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, cast

from .contracts import AgentService
from .errors import AgentCredentialFailureError
from .provider_errors import ProviderErrorObservation
from .roles import AgentRole
from .session import (
    ProviderSessionStateRequest,
    RunKind,
    normalize_state_dir_relpath,
)


class AuthSeedingRequirement(Enum):
    REQUIRED = "required"
    NOT_REQUIRED = "not_required"


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
            raise AgentCredentialFailureError(
                self.missing_source_message,
                status_code=401,
                service_name="codex",
                observations=(
                    ProviderErrorObservation(
                        service_name="codex",
                        raw_provider_text=self.missing_source_message,
                        source_stream="pre-dispatch host check",
                        status_code=401,
                    ),
                ),
            )
        return self.source

    def apply(self) -> None:
        if self.destination.exists():
            return
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.require_source(), self.destination)


class RecoveredSessionIdPersistence(Enum):
    PERSIST = "persist"
    SKIP = "skip"


@dataclasses.dataclass(frozen=True)
class ProviderSessionDecision:
    run_kind: RunKind
    provider_session_id: str | None
    state_dir_relpath: str | None
    state_dir_path: Path | None
    recovered_session_id_persistence: RecoveredSessionIdPersistence
    service_state_dir: Path | None = None
    exact_transcript_match: bool = False
    auth_seeding_requirement: AuthSeedingRequirement = (
        AuthSeedingRequirement.NOT_REQUIRED
    )
    auth_seed_action: LocalAuthSeedAction | None = None

    def container_state_dir(self, *, service_name: str) -> Path | None:
        if (
            service_name == "opencode"
            and self.run_kind is RunKind.RESUME
            and self.service_state_dir is not None
        ):
            return self.service_state_dir
        return self.state_dir_path

    def container_state_dir_path(
        self,
        *,
        worktree: Path,
        service_name: str,
        container_workspace: str,
    ) -> str | None:
        container_state_dir = self.container_state_dir(service_name=service_name)
        if container_state_dir is not None:
            try:
                container_relpath = container_state_dir.relative_to(worktree)
            except ValueError:
                pass
            else:
                return f"{container_workspace}/{container_relpath.as_posix()}/"
        if self.state_dir_relpath is None:
            return None
        return f"{container_workspace}/{self.state_dir_relpath}"


class ServiceSessionStateLike(Protocol):
    state_dir: Path | None
    has_resumable_provider_state: bool
    state_dir_relpath: str | None


@dataclasses.dataclass(frozen=True)
class ProviderRunStatePlanRequest:
    worktree: Path
    role: AgentRole
    namespace: str
    service: AgentService
    role_session: Any


@dataclasses.dataclass
class ProviderRunStatePlan:
    role_session: Any = dataclasses.field(repr=False, compare=False)
    service_name: str
    run_kind: RunKind
    provider_state_dir: Path | None
    provider_state_dir_relpath: str | None
    provider_session_id: str | None
    requires_host_codex_auth: bool
    recovered_session_id_persistence: RecoveredSessionIdPersistence
    service_state_dir: Path | None = None
    exact_transcript_match: bool = False
    auth_seed_action: LocalAuthSeedAction | None = None

    @property
    def auth_seeding_requirement(self) -> AuthSeedingRequirement:
        if self.requires_host_codex_auth:
            return AuthSeedingRequirement.REQUIRED
        return AuthSeedingRequirement.NOT_REQUIRED

    def provider_session_decision(self) -> ProviderSessionDecision:
        return ProviderSessionDecision(
            run_kind=self.run_kind,
            provider_session_id=self.provider_session_id,
            state_dir_relpath=self.provider_state_dir_relpath,
            state_dir_path=self.provider_state_dir,
            recovered_session_id_persistence=self.recovered_session_id_persistence,
            service_state_dir=self.service_state_dir,
            exact_transcript_match=self.exact_transcript_match,
            auth_seeding_requirement=self.auth_seeding_requirement,
            auth_seed_action=self.auth_seed_action,
        )

    def provider_state_dir_container_path(
        self,
        *,
        worktree: Path,
        container_workspace: str,
    ) -> str | None:
        return self.provider_session_decision().container_state_dir_path(
            worktree=worktree,
            service_name=self.service_name,
            container_workspace=container_workspace,
        )

    def prepare_provider_state_dir(self) -> None:
        if self.provider_state_dir is not None:
            self.provider_state_dir.mkdir(parents=True, exist_ok=True)
        if self.auth_seed_action is not None:
            self.auth_seed_action.apply()

    def prepared_provider_session_id(self) -> str | None:
        provider_session_id = self.provider_session_id
        if provider_session_id is None:
            return None
        if (
            self.recovered_session_id_persistence
            is RecoveredSessionIdPersistence.PERSIST
        ):
            self.remember_provider_session_id(provider_session_id)
        return provider_session_id

    def remember_provider_session_id(self, provider_session_id: str) -> None:
        self.provider_session_id = provider_session_id
        if self.service_name == "opencode" and self.service_state_dir is not None:
            session_id_path = self.service_state_dir / "session_id"
            session_id_path.parent.mkdir(parents=True, exist_ok=True)
            session_id_path.write_text(provider_session_id, encoding="utf-8")
        if self.service_name not in {"codex", "opencode"}:
            return
        self.role_session.save_service_session_id(
            self.service_name,
            provider_session_id,
        )

    def record_successful_run(self, provider_session_id: str | None) -> None:
        self.role_session.record_successful_provider_session_metadata(
            self.service_name,
            provider_session_id,
        )


ProviderSessionPlanRequest = ProviderRunStatePlanRequest


def record_observed_provider_session_id(
    *,
    provider_run_state_plan: ProviderRunStatePlan,
    provider_session_id: str,
) -> None:
    provider_run_state_plan.remember_provider_session_id(provider_session_id)


def record_successful_provider_session_metadata(
    *,
    provider_run_state_plan: ProviderRunStatePlan,
    provider_session_id: str | None,
) -> None:
    provider_run_state_plan.record_successful_run(provider_session_id)


def plan_provider_session(
    request: ProviderRunStatePlanRequest,
) -> ProviderSessionDecision:
    return plan_provider_run_state(request).provider_session_decision()


def plan_provider_run_state(
    request: ProviderRunStatePlanRequest,
) -> ProviderRunStatePlan:
    service_state = cast(
        ServiceSessionStateLike,
        request.role_session.service_session_state(request.service),
    )
    raw_state_dir_relpath = request.service.state_dir_relpath(
        request.role,
        request.namespace,
    )
    state_dir_relpath = normalize_state_dir_relpath(
        request.role,
        request.namespace,
        request.service.name,
        raw_state_dir_relpath,
    )
    host_state_dir = service_state.state_dir
    if state_dir_relpath is not None and state_dir_relpath != raw_state_dir_relpath:
        host_state_dir = request.worktree / state_dir_relpath.rstrip("/")

    provider_session_state = request.service.provider_session_state(
        ProviderSessionStateRequest(
            role_session=cast(Any, request.role_session),
            provider_state_dir=host_state_dir,
            has_resumable_provider_state=service_state.has_resumable_provider_state,
            state_dir_relpath=state_dir_relpath,
            require_exact_transcript_match=True,
            preferred_provider_session_id=(
                request.role_session.session_uuid()
                if request.service.name == "claude"
                else None
            ),
        )
    )
    recovered_session_id_persistence = RecoveredSessionIdPersistence.SKIP
    if provider_session_state.persist_provider_session_id:
        recovered_session_id_persistence = RecoveredSessionIdPersistence.PERSIST
    selected_provider_state_dir = (
        provider_session_state.state_dir_path or host_state_dir
    )
    auth_seeding_requirement = _plan_auth_seeding_requirement(
        service_name=request.service.name,
        provider_state_dir=selected_provider_state_dir,
    )
    return ProviderRunStatePlan(
        role_session=request.role_session,
        service_name=request.service.name,
        run_kind=provider_session_state.run_kind,
        provider_session_id=provider_session_state.provider_session_id,
        provider_state_dir=selected_provider_state_dir,
        provider_state_dir_relpath=(
            provider_session_state.state_dir_relpath or state_dir_relpath
        ),
        requires_host_codex_auth=(
            auth_seeding_requirement is AuthSeedingRequirement.REQUIRED
        ),
        recovered_session_id_persistence=recovered_session_id_persistence,
        service_state_dir=service_state.state_dir,
        exact_transcript_match=provider_session_state.exact_transcript_match,
        auth_seed_action=_plan_auth_seed_action(
            service_name=request.service.name,
            provider_state_dir=selected_provider_state_dir,
        ),
    )


def _plan_auth_seeding_requirement(
    *,
    service_name: str,
    provider_state_dir: Path | None,
) -> AuthSeedingRequirement:
    if service_name != "codex":
        return AuthSeedingRequirement.NOT_REQUIRED
    if provider_state_dir is None or (provider_state_dir / "auth.json").exists():
        return AuthSeedingRequirement.NOT_REQUIRED
    return AuthSeedingRequirement.REQUIRED


def _plan_auth_seed_action(
    *,
    service_name: str,
    provider_state_dir: Path | None,
) -> LocalAuthSeedAction | None:
    if (
        _plan_auth_seeding_requirement(
            service_name=service_name,
            provider_state_dir=provider_state_dir,
        )
        is AuthSeedingRequirement.NOT_REQUIRED
    ):
        return None
    if provider_state_dir is None:
        return None
    return LocalAuthSeedAction(
        source=Path.home() / ".codex" / "auth.json",
        destination=provider_state_dir / "auth.json",
    )


__all__ = [
    "AuthSeedingRequirement",
    "LocalAuthSeedAction",
    "ProviderRunStatePlan",
    "ProviderRunStatePlanRequest",
    "ProviderSessionDecision",
    "ProviderSessionPlanRequest",
    "RecoveredSessionIdPersistence",
    "plan_provider_run_state",
    "plan_provider_session",
    "record_observed_provider_session_id",
    "record_successful_provider_session_metadata",
]
