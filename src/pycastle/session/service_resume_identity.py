from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ._provider_session_sidecars import load_state_dir_provider_session_id


class ServiceResumeIdentityStore(Protocol):
    def session_uuid(self) -> str: ...

    def service_session_id(self, service_name: str) -> str | None: ...

    def save_service_session_id(self, service_name: str, session_id: str) -> None: ...

    def service_session_metadata(self, service_name: str) -> dict[str, str] | None: ...

    def exact_transcript_service_name(self) -> str | None: ...


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
        role_session.exact_transcript_service_name() == service_name
        and metadata is not None
        and metadata["provider_session_id"] == provider_session_id
        and _is_exact_resumable_provider_session(
            service_name,
            provider_session_id,
            provider_state_dir,
        )
    )


def _provider_session_id_from_state_dir(
    service_name: str,
    state_dir: Path | None,
) -> str | None:
    provider_session_id = load_state_dir_provider_session_id(state_dir, service_name)
    if provider_session_id is not None:
        return provider_session_id
    if service_name != "codex":
        return None
    return _recover_codex_rollout_thread_id(state_dir)


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


def _is_exact_resumable_provider_session(
    service_name: str,
    provider_session_id: str | None,
    provider_state_dir: Path | None,
) -> bool:
    if provider_session_id is None or provider_state_dir is None:
        return False
    if service_name not in {"codex", "opencode"}:
        return True
    exact_provider_session_id = _exact_provider_session_id_from_state_dir(
        service_name,
        provider_state_dir,
    )
    return exact_provider_session_id == provider_session_id


def _exact_provider_session_id_from_state_dir(
    service_name: str,
    state_dir: Path | None,
) -> str | None:
    if service_name == "codex":
        return _recover_codex_rollout_thread_id(state_dir)
    if service_name == "opencode":
        return load_state_dir_provider_session_id(state_dir, service_name)
    return None
