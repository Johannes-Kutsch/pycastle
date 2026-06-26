from __future__ import annotations

import dataclasses
import uuid
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol

from .agents.output_protocol import AgentRole

if TYPE_CHECKING:
    from pycastle.session_planning import (
        AuthSeedingRequirement,
        LocalAuthSeedAction,
    )
else:
    AuthSeedingRequirement = object
    LocalAuthSeedAction = object


class ServiceResumeIdentityStore(Protocol):
    def save_service_session_id(self, service_name: str, session_id: str) -> None: ...

    def service_session_metadata(self, service_name: str) -> dict[str, str] | None: ...

    def exact_transcript_service_name(self) -> str | None: ...


_DEFAULT_PROVIDER_SESSION_ID_FILENAME = "thread_id"
_NAMESPACE = uuid.NAMESPACE_DNS
_SESSION_UUID_SEED_FILENAME = "_session_uuid_seed"


def session_uuid(
    worktree: Path,
    role_name: str,
    namespace: str = "",
    *,
    session_root: str = ".pycastle-session",
) -> str:
    role_key = (
        f"pycastle.{role_name}.{namespace}" if namespace else f"pycastle.{role_name}"
    )
    role_ns = uuid.uuid5(_NAMESPACE, role_key)
    session_id = uuid.uuid5(
        role_ns,
        f"{worktree.resolve()}:{_ensure_session_uuid_seed(worktree, role_name, namespace, session_root=session_root)}",
    )
    return str(session_id)


def _role_session_uuid_seed_path(
    worktree: Path,
    role_name: str,
    namespace: str = "",
    *,
    session_root: str = ".pycastle-session",
) -> Path:
    base = worktree / session_root / role_name
    return (base / namespace if namespace else base) / _SESSION_UUID_SEED_FILENAME


def _ensure_session_uuid_seed(
    worktree: Path,
    role_name: str,
    namespace: str = "",
    *,
    session_root: str = ".pycastle-session",
) -> str:
    path = _role_session_uuid_seed_path(
        worktree,
        role_name,
        namespace,
        session_root=session_root,
    )
    if path.is_file():
        seed = path.read_text(encoding="utf-8").strip()
        if seed:
            return seed
    seed = str(uuid.uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(seed, encoding="utf-8")
    return seed


class RunKind(Enum):
    FRESH = "fresh"
    RESUME = "resume"


@dataclasses.dataclass(frozen=True)
class ProviderSessionSelection:
    provider_session_id: str | None
    persist_provider_session_id: bool = False


@dataclasses.dataclass(frozen=True)
class ProviderSessionPreferencesRequest:
    role_session: ServiceResumeIdentityStore
    provider_state_dir: Path | None
    has_resumable_provider_state: bool
    state_dir_relpath: str | None = None
    preferred_provider_session_id: str | None = None
    force_resume: bool = False


@dataclasses.dataclass(frozen=True)
class ProviderSessionPreferences:
    preferred_provider_session_id: str | None = None


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
    use_service_state_dir_for_container: bool = False


def provider_state_relpath(
    role: AgentRole,
    provider_name: str,
    namespace: str = "",
    *,
    session_root: str = "",
) -> str:
    base = f"{role.value}/{provider_name}/"
    if namespace:
        base = f"{role.value}/{namespace}/{provider_name}/"
    return f"{session_root}/{base}" if session_root else base


def normalize_state_dir_relpath(
    role: AgentRole,
    namespace: str,
    service_name: str,
    state_dir_relpath: str | None,
    *,
    session_root: str | None = None,
) -> str | None:
    if state_dir_relpath is None or not namespace:
        return state_dir_relpath
    session_root = session_root or _session_root_for_relpath(state_dir_relpath)
    legacy_relpath = provider_state_relpath(
        role, service_name, session_root=session_root
    )
    if state_dir_relpath == legacy_relpath:
        return provider_state_relpath(
            role,
            service_name,
            namespace,
            session_root=session_root,
        )
    return state_dir_relpath


def _session_root_for_relpath(state_dir_relpath: str) -> str:
    stripped = state_dir_relpath.strip("/")
    parts = stripped.split("/")
    if len(parts) >= 3:
        return parts[0]
    return ""


def provider_state_session_id_path(
    state_dir: Path,
    service_name: str,
    *,
    session_id_filename: str = _DEFAULT_PROVIDER_SESSION_ID_FILENAME,
) -> Path:
    del service_name
    return state_dir / session_id_filename


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
    *,
    session_id_filename: str = _DEFAULT_PROVIDER_SESSION_ID_FILENAME,
) -> str | None:
    if state_dir is None:
        return None
    return load_provider_state_session_id(
        provider_state_session_id_path(
            state_dir,
            service_name,
            session_id_filename=session_id_filename,
        )
    )


def select_resumable_provider_session_id(
    role_session: ServiceResumeIdentityStore,
    service_name: str,
    *,
    provider_state_dir: Path | None,
    has_resumable_provider_state: bool,
    recover_provider_session_id: Callable[[Path | None], str | None] | None = None,
) -> ProviderSessionSelection:
    if not has_resumable_provider_state:
        return ProviderSessionSelection(provider_session_id=None)

    role_session_path = getattr(role_session, "path", None)
    from .session.service_session_store import load_service_session_id

    provider_session_id = (
        load_service_session_id(role_session_path, service_name)
        if isinstance(role_session_path, Path)
        else None
    )
    if provider_session_id is None:
        service_session_id = getattr(role_session, "service_session_id", None)
        if callable(service_session_id):
            provider_session_id = service_session_id(service_name)
    if provider_session_id is not None:
        return ProviderSessionSelection(provider_session_id=provider_session_id)

    recover_provider_session_id = recover_provider_session_id or (
        lambda state_dir: load_state_dir_provider_session_id(
            state_dir,
            service_name,
        )
    )
    provider_session_id = recover_provider_session_id(provider_state_dir)
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
    exact_provider_session_matcher: Callable[[str | None, Path | None], bool]
    | None = None,
) -> bool:
    metadata = role_session.service_session_metadata(service_name)
    if (
        role_session.exact_transcript_service_name() != service_name
        or metadata is None
        or metadata["provider_session_id"] != provider_session_id
    ):
        return False
    if exact_provider_session_matcher is not None:
        return exact_provider_session_matcher(provider_session_id, provider_state_dir)
    return provider_session_id is not None and provider_state_dir is not None


__all__ = [
    "ProviderSessionPreferences",
    "ProviderSessionPreferencesRequest",
    "ProviderSessionSelection",
    "ProviderSessionState",
    "ProviderSessionStateRequest",
    "RunKind",
    "is_exact_resumable_service_session",
    "load_provider_state_session_id",
    "load_state_dir_provider_session_id",
    "normalize_state_dir_relpath",
    "provider_state_session_id_path",
    "provider_state_relpath",
    "select_resumable_provider_session_id",
]
