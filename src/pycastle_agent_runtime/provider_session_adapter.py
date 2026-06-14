from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Protocol

from .contracts import ProviderSessionRecordingStore, ProviderStatePreparationAction
from .roles import AgentRole
from .session import (
    ProviderSessionPreferences,
    ProviderSessionPreferencesRequest,
    ProviderSessionState,
    ProviderSessionStateRequest,
)


@dataclasses.dataclass(frozen=True)
class ProviderSessionPlanningRequest:
    worktree: Path
    role: AgentRole
    namespace: str


@dataclasses.dataclass(frozen=True)
class ProviderSessionPlanningFacts:
    state_dir_relpath: str | None
    provider_state_dir: Path | None
    has_resumable_provider_state: bool


class ProviderSessionService(Protocol):
    @property
    def name(self) -> str: ...

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None: ...

    def is_resumable(self, state_dir: Path) -> bool: ...

    def provider_session_preferences(
        self, request: ProviderSessionPreferencesRequest
    ) -> ProviderSessionPreferences: ...

    def provider_session_state(
        self, request: ProviderSessionStateRequest
    ) -> ProviderSessionState: ...


class ProviderSessionAdapter(Protocol):
    @property
    def service_name(self) -> str: ...

    def provider_session_planning_facts(
        self, request: ProviderSessionPlanningRequest
    ) -> ProviderSessionPlanningFacts: ...

    def provider_session_preferences(
        self, request: ProviderSessionPreferencesRequest
    ) -> ProviderSessionPreferences: ...

    def provider_session_state(
        self, request: ProviderSessionStateRequest
    ) -> ProviderSessionState: ...

    def prepare_local_provider_run_state(
        self,
        provider_state_dir: Path | None,
        auth_seed_action: ProviderStatePreparationAction | None = None,
    ) -> None: ...

    def record_provider_session_id(
        self,
        *,
        role_session: ProviderSessionRecordingStore,
        provider_session_id: str,
        service_state_dir: Path | None = None,
    ) -> None: ...

    def recover_provider_session_id(
        self,
        provider_state_dir: Path | None,
    ) -> str | None: ...

    def is_exact_resumable_provider_session(
        self,
        *,
        provider_session_id: str | None,
        provider_state_dir: Path | None,
    ) -> bool: ...


class _LegacyProviderSessionAdapter:
    def __init__(
        self,
        *,
        service_name: str,
        service: ProviderSessionService | None = None,
    ) -> None:
        self._service_name = service_name
        self._service = service

    @property
    def service_name(self) -> str:
        return self._service_name

    def provider_session_planning_facts(
        self, request: ProviderSessionPlanningRequest
    ) -> ProviderSessionPlanningFacts:
        state_dir_relpath = self._require_service().state_dir_relpath(
            request.role,
            request.namespace,
        )
        provider_state_dir = _host_state_dir(request.worktree, state_dir_relpath)
        has_resumable_provider_state = (
            provider_state_dir is not None
            and self._require_service().is_resumable(provider_state_dir)
        )
        return ProviderSessionPlanningFacts(
            state_dir_relpath=state_dir_relpath,
            provider_state_dir=provider_state_dir,
            has_resumable_provider_state=has_resumable_provider_state,
        )

    def provider_session_preferences(
        self, request: ProviderSessionPreferencesRequest
    ) -> ProviderSessionPreferences:
        return self._require_service().provider_session_preferences(request)

    def provider_session_state(
        self, request: ProviderSessionStateRequest
    ) -> ProviderSessionState:
        return self._require_service().provider_session_state(request)

    def prepare_local_provider_run_state(
        self,
        provider_state_dir: Path | None,
        auth_seed_action: ProviderStatePreparationAction | None = None,
    ) -> None:
        if provider_state_dir is not None:
            provider_state_dir.mkdir(parents=True, exist_ok=True)
        if auth_seed_action is not None:
            auth_seed_action.apply()

    def record_provider_session_id(
        self,
        *,
        role_session: ProviderSessionRecordingStore,
        provider_session_id: str,
        service_state_dir: Path | None = None,
    ) -> None:
        role_session.save_service_session_id(self._service_name, provider_session_id)
        del service_state_dir

    def recover_provider_session_id(
        self,
        provider_state_dir: Path | None,
    ) -> str | None:
        del provider_state_dir
        return None

    def is_exact_resumable_provider_session(
        self,
        *,
        provider_session_id: str | None,
        provider_state_dir: Path | None,
    ) -> bool:
        return provider_session_id is not None and provider_state_dir is not None

    def _require_service(self) -> ProviderSessionService:
        if self._service is None:
            raise RuntimeError(
                "provider session selection requires a concrete provider session service"
            )
        return self._service


def legacy_provider_session_adapter(
    service: ProviderSessionService,
) -> ProviderSessionAdapter:
    return _LegacyProviderSessionAdapter(service_name=service.name, service=service)


def legacy_provider_session_metadata_adapter(
    service_name: str,
) -> ProviderSessionAdapter:
    return _LegacyProviderSessionAdapter(service_name=service_name)


def _host_state_dir(worktree: Path, state_dir_relpath: str | None) -> Path | None:
    if state_dir_relpath is None:
        return None
    return worktree / state_dir_relpath.rstrip("/")


__all__ = [
    "ProviderSessionAdapter",
    "ProviderSessionPlanningFacts",
    "ProviderSessionPlanningRequest",
    "ProviderSessionService",
]
