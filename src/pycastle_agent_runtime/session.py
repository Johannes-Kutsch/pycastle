from __future__ import annotations

import dataclasses
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .roles import AgentRole
    from pycastle_agent_runtime.session_planning import (
        AuthSeedingRequirement,
        LocalAuthSeedAction,
    )
else:
    AuthSeedingRequirement = object
    LocalAuthSeedAction = object


class ServiceResumeIdentityStore(Protocol):
    def session_uuid(self) -> str: ...

    def service_session_id(self, service_name: str) -> str | None: ...

    def save_service_session_id(self, service_name: str, session_id: str) -> None: ...

    def service_session_metadata(self, service_name: str) -> dict[str, str] | None: ...

    def exact_transcript_service_name(self) -> str | None: ...


SESSION_DIR_NAME = ".pycastle-session"


class RunKind(Enum):
    FRESH = "fresh"
    RESUME = "resume"


@dataclasses.dataclass(frozen=True)
class ProviderSessionSelection:
    provider_session_id: str | None
    persist_provider_session_id: bool = False


@dataclasses.dataclass(frozen=True)
class ProviderSessionStateRequest:
    role_session: ServiceResumeIdentityStore
    provider_state_dir: Path | None
    has_resumable_provider_state: bool
    state_dir_relpath: str | None = None
    require_exact_transcript_match: bool = False
    preferred_provider_session_id: str | None = None
    force_resume: bool = False


@dataclasses.dataclass(frozen=True)
class ProviderSessionState:
    run_kind: RunKind
    provider_session_id: str | None
    state_dir_relpath: str | None = None
    state_dir_path: Path | None = None
    exact_transcript_match: bool = False
    persist_provider_session_id: bool = False
    auth_seeding_requirement: AuthSeedingRequirement | None = None
    auth_seed_action: LocalAuthSeedAction | None = None
    allow_protocol_reprompt: bool = True


def provider_state_relpath(
    role: "AgentRole",
    provider_name: str,
    namespace: str = "",
) -> str:
    if namespace:
        return f"{SESSION_DIR_NAME}/{role.value}/{namespace}/{provider_name}/"
    return f"{SESSION_DIR_NAME}/{role.value}/{provider_name}/"


def normalize_state_dir_relpath(
    role: "AgentRole",
    namespace: str,
    service_name: str,
    state_dir_relpath: str | None,
) -> str | None:
    if state_dir_relpath is None or not namespace:
        return state_dir_relpath
    legacy_relpath = provider_state_relpath(role, service_name)
    if state_dir_relpath == legacy_relpath:
        return provider_state_relpath(role, service_name, namespace)
    return state_dir_relpath


_SERVICE_SESSION_ID_FILENAMES = {"codex": "thread_id", "opencode": "session_id"}


def provider_state_session_id_path(state_dir: Path, service_name: str) -> Path:
    filename = _SERVICE_SESSION_ID_FILENAMES.get(service_name, "thread_id")
    return state_dir / filename


def load_provider_state_session_id(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return value or None


def load_state_dir_provider_session_id(
    state_dir: Path | None,
    service_name: str,
) -> str | None:
    if state_dir is None:
        return None
    return load_provider_state_session_id(
        provider_state_session_id_path(state_dir, service_name)
    )


def select_resumable_provider_session_id(
    role_session: ServiceResumeIdentityStore,
    service_name: str,
    *,
    provider_state_dir: Path | None,
    has_resumable_provider_state: bool,
) -> ProviderSessionSelection:
    if not has_resumable_provider_state:
        return ProviderSessionSelection(provider_session_id=None)

    provider_session_id = role_session.service_session_id(service_name)
    if provider_session_id is not None:
        return ProviderSessionSelection(provider_session_id=provider_session_id)

    provider_session_id = load_state_dir_provider_session_id(
        provider_state_dir,
        service_name,
    )
    if provider_session_id is None:
        return ProviderSessionSelection(provider_session_id=None)

    role_session.save_service_session_id(service_name, provider_session_id)
    return ProviderSessionSelection(
        provider_session_id=provider_session_id,
        persist_provider_session_id=True,
    )


__all__ = [
    "ProviderSessionSelection",
    "ProviderSessionState",
    "ProviderSessionStateRequest",
    "RunKind",
    "SESSION_DIR_NAME",
    "load_provider_state_session_id",
    "load_state_dir_provider_session_id",
    "normalize_state_dir_relpath",
    "provider_state_session_id_path",
    "provider_state_relpath",
    "select_resumable_provider_session_id",
]
