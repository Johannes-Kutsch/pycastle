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


__all__ = [
    "ProviderSessionState",
    "ProviderSessionStateRequest",
    "RunKind",
    "SESSION_DIR_NAME",
    "normalize_state_dir_relpath",
    "provider_state_relpath",
]
