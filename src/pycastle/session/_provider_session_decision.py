from __future__ import annotations

import dataclasses
import shutil
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from ..errors import HardAgentError

if TYPE_CHECKING:
    from .resume import RunKind


class AuthSeedingRequirement(Enum):
    REQUIRED = "required"
    NOT_REQUIRED = "not_required"


@dataclasses.dataclass(frozen=True)
class LocalAuthSeedAction:
    source: Path
    destination: Path
    missing_source_message: str = dataclasses.field(
        default="Codex authentication missing: run `codex login` on the host.",
        compare=False,
    )

    def require_source(self) -> Path:
        if not self.source.exists():
            raise HardAgentError(
                self.missing_source_message,
                status_code=401,
            )
        return self.source

    def apply(self) -> None:
        if self.destination.exists():
            return
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.require_source(), self.destination)


class RecoveredSessionIdPersistence(Enum):
    PERSIST = "persist"
    SKIP = "skip"


@dataclasses.dataclass(frozen=True)
class ProviderSessionDecision:
    run_kind: RunKind
    provider_session_id: str | None
    state_dir_relpath: str | None
    state_dir_path: Path | None
    recovered_session_id_persistence: RecoveredSessionIdPersistence
    service_state_dir: Path | None = None
    exact_transcript_match: bool = False
    auth_seeding_requirement: AuthSeedingRequirement = (
        AuthSeedingRequirement.NOT_REQUIRED
    )
    auth_seed_action: LocalAuthSeedAction | None = None

    def container_state_dir(self, *, service_name: str) -> Path | None:
        if (
            service_name == "opencode"
            and getattr(self.run_kind, "value", None) == "resume"
            and self.service_state_dir is not None
        ):
            return self.service_state_dir
        return self.state_dir_path

    def container_state_dir_path(
        self,
        *,
        worktree: Path,
        service_name: str,
        container_workspace: str,
    ) -> str | None:
        container_state_dir = self.container_state_dir(service_name=service_name)
        if container_state_dir is not None:
            try:
                container_relpath = container_state_dir.relative_to(worktree)
            except ValueError:
                pass
            else:
                return f"{container_workspace}/{container_relpath.as_posix()}/"
        if self.state_dir_relpath is None:
            return None
        return f"{container_workspace}/{self.state_dir_relpath}"


__all__ = [
    "AuthSeedingRequirement",
    "LocalAuthSeedAction",
    "ProviderSessionDecision",
    "RecoveredSessionIdPersistence",
]
