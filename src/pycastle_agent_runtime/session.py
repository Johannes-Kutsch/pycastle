from __future__ import annotations

import dataclasses
import json
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
_SERVICE_SESSION_ID_FILENAMES = {"codex": "thread_id", "opencode": "session_id"}


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


def recover_state_dir_provider_session_id(
    state_dir: Path | None,
    service_name: str,
) -> str | None:
    if service_name != "codex":
        return None
    return _recover_codex_rollout_thread_id(state_dir)


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


def is_exact_resumable_service_session(
    role_session: ServiceResumeIdentityStore,
    service_name: str,
    *,
    provider_session_id: str | None,
    provider_state_dir: Path | None,
) -> bool:
    metadata = role_session.service_session_metadata(service_name)
    return (
        role_session.exact_transcript_service_name() == service_name
        and metadata is not None
        and metadata["provider_session_id"] == provider_session_id
        and _is_exact_resumable_provider_session(
            service_name,
            provider_session_id,
            provider_state_dir,
        )
    )


def _is_exact_resumable_provider_session(
    service_name: str,
    provider_session_id: str | None,
    provider_state_dir: Path | None,
) -> bool:
    if provider_session_id is None or provider_state_dir is None:
        return False
    if service_name != "codex":
        return True
    exact_provider_session_id = recover_state_dir_provider_session_id(
        provider_state_dir,
        service_name,
    )
    return exact_provider_session_id == provider_session_id


def _recover_codex_rollout_thread_id(state_dir: Path | None) -> str | None:
    if state_dir is None:
        return None
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
            if not isinstance(obj, dict) or obj.get("type") != "thread.started":
                continue
            thread_id = obj.get("thread_id")
            if isinstance(thread_id, str) and thread_id.strip():
                found.add(thread_id.strip())

    return next(iter(found)) if len(found) == 1 else None


__all__ = [
    "ProviderSessionSelection",
    "ProviderSessionState",
    "ProviderSessionStateRequest",
    "RunKind",
    "SESSION_DIR_NAME",
    "is_exact_resumable_service_session",
    "load_provider_state_session_id",
    "load_state_dir_provider_session_id",
    "normalize_state_dir_relpath",
    "provider_state_session_id_path",
    "provider_state_relpath",
    "recover_state_dir_provider_session_id",
    "select_resumable_provider_session_id",
]
