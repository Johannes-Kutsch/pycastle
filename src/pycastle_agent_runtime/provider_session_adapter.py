from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .contracts import ProviderSessionRecordingStore, ProviderStatePreparationAction
from .session import (
    ProviderSessionPreferences,
    ProviderSessionPreferencesRequest,
    ProviderSessionState,
    ProviderSessionStateRequest,
)


class ProviderSessionService(Protocol):
    @property
    def name(self) -> str: ...

    def provider_session_preferences(
        self, request: ProviderSessionPreferencesRequest
    ) -> ProviderSessionPreferences: ...

    def provider_session_state(
        self, request: ProviderSessionStateRequest
    ) -> ProviderSessionState: ...


class ProviderSessionAdapter(Protocol):
    @property
    def service_name(self) -> str: ...

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
        if self._service_name == "opencode" and service_state_dir is not None:
            session_id_path = service_state_dir / "session_id"
            session_id_path.parent.mkdir(parents=True, exist_ok=True)
            session_id_path.write_text(provider_session_id, encoding="utf-8")
        if self._service_name not in {"codex", "opencode"}:
            return
        role_session.save_service_session_id(self._service_name, provider_session_id)

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


__all__ = [
    "ProviderSessionAdapter",
    "ProviderSessionService",
]
