from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

from ..session.service_resume_identity import ServiceResumeIdentityStore

if TYPE_CHECKING:
    from ..session.resume import RunKind


@dataclasses.dataclass(frozen=True)
class ProviderSessionStateRequest:
    role_session: ServiceResumeIdentityStore
    provider_state_dir: Path | None
    has_resumable_provider_state: bool
    require_exact_transcript_match: bool = False
    preferred_provider_session_id: str | None = None
    force_resume: bool = False


@dataclasses.dataclass(frozen=True)
class ProviderSessionState:
    run_kind: RunKind
    provider_session_id: str | None
    exact_transcript_match: bool = False
    persist_provider_session_id: bool = False


__all__ = [
    "ProviderSessionState",
    "ProviderSessionStateRequest",
]
