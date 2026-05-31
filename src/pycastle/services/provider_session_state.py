from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..session._provider_session_decision import (
        AuthSeedingRequirement,
        LocalAuthSeedAction,
    )
    from ..session.service_resume_identity import ServiceResumeIdentityStore
    from ..session.resume import RunKind
else:
    AuthSeedingRequirement = object
    LocalAuthSeedAction = object


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


__all__ = [
    "ProviderSessionState",
    "ProviderSessionStateRequest",
]
