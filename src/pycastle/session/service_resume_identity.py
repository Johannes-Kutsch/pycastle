from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ..services import ServiceRegistry
    from ..services.agent_service import AgentService


class ServiceResumeIdentityStore(Protocol):
    def service_session_id(self, service_name: str) -> str | None: ...

    def save_service_session_id(self, service_name: str, session_id: str) -> None: ...

    def service_session_metadata(self, service_name: str) -> dict[str, str] | None: ...


class ExactTranscriptHandoffStore(ServiceResumeIdentityStore, Protocol):
    def service_session_state(self, service: AgentService) -> Any: ...

    def session_uuid(self) -> str: ...


@dataclass(frozen=True)
class ProviderSessionSelection:
    provider_session_id: str | None
    persist_provider_session_id: bool = False


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

    provider_session_id = _provider_session_id_from_state_dir(
        service_name,
        provider_state_dir,
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
        metadata is not None
        and metadata["provider_session_id"] == provider_session_id
        and _is_exact_resumable_provider_session(
            service_name,
            provider_session_id,
            provider_state_dir,
        )
    )


def has_exact_transcript_handoff_for_selected_service(
    role_session: ExactTranscriptHandoffStore,
    registry: ServiceRegistry | None,
    service_name: str,
) -> bool:
    if registry is None or not service_name:
        return False
    service = registry[service_name]
    if service is None:
        return False

    state = role_session.service_session_state(service)
    if not state.has_resumable_provider_state:
        return False

    if service_name == "claude":
        # Claude session identity is UUID-derived, not written to a provider-owned file.
        # `capture_provider_session_id` skips `save_service_session_id` for Claude, so
        # the file-based lookup used by `select_resumable_provider_session_id` would
        # always return None. Use the same UUID that `exact_transcript_handoff` uses.
        provider_session_id: str | None = role_session.session_uuid()
    else:
        selection = select_resumable_provider_session_id(
            role_session,
            service_name,
            provider_state_dir=state.state_dir,
            has_resumable_provider_state=state.has_resumable_provider_state,
        )
        if (
            selection.provider_session_id is None
            or selection.persist_provider_session_id
        ):
            return False
        provider_session_id = selection.provider_session_id

    return is_exact_resumable_service_session(
        role_session,
        service_name,
        provider_session_id=provider_session_id,
        provider_state_dir=state.state_dir,
    )


def _codex_thread_id_from_rollouts(state_dir: Path) -> str | None:
    from ._provider_session_state import recover_codex_rollout_thread_id

    return recover_codex_rollout_thread_id(state_dir)


def _provider_session_id_from_state_dir(
    service_name: str,
    state_dir: Path | None,
) -> str | None:
    if state_dir is None:
        return None
    if service_name == "codex":
        return _codex_thread_id_from_rollouts(state_dir)

    session_id_path = state_dir / {
        "codex": "thread_id",
        "opencode": "session_id",
    }.get(service_name, "thread_id")
    if not session_id_path.is_file():
        return None
    try:
        value = session_id_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return value or None


def _is_exact_resumable_provider_session(
    service_name: str,
    provider_session_id: str | None,
    provider_state_dir: Path | None,
) -> bool:
    if provider_session_id is None or provider_state_dir is None:
        return False
    if service_name == "codex":
        return _codex_thread_id_from_rollouts(provider_state_dir) == provider_session_id
    return True
