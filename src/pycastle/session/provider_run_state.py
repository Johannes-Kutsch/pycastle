from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .resume import RunKind


class ProviderFreshFallbackReason(Enum):
    UNRECOVERABLE_IDENTITY = "unrecoverable_identity"


@dataclass(frozen=True)
class ProviderRunState:
    run_kind: RunKind
    provider_session_id: str | None
    persist_provider_session_id: bool = field(default=False, compare=False)
    provider_state_dir: Path | None = None
    fresh_fallback_reason: ProviderFreshFallbackReason | None = None
