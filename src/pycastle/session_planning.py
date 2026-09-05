from __future__ import annotations

import dataclasses
import shutil
from enum import Enum
from pathlib import Path


class AuthSeedingRequirement(Enum):
    REQUIRED = "required"
    NOT_REQUIRED = "not_required"


@dataclasses.dataclass(frozen=True)
class LocalAuthSeedAction:
    source: Path
    destination: Path
    missing_source_message: str | None = dataclasses.field(default=None, compare=False)
    missing_source_service_name: str | None = dataclasses.field(
        default=None,
        compare=False,
    )
    missing_source_status_code: int | None = dataclasses.field(
        default=None,
        compare=False,
    )
    missing_source_classification: str | None = dataclasses.field(
        default=None,
        compare=False,
    )

    def require_source(self) -> Path:
        if not self.source.exists():
            if (
                self.missing_source_message is None
                or self.missing_source_service_name is None
            ):
                raise FileNotFoundError(self.source)
            from agent_runtime.errors import AgentCredentialFailureError

            raise AgentCredentialFailureError(
                self.missing_source_message,
                service_name=self.missing_source_service_name,
                classification=self.missing_source_classification,
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


__all__ = [
    "AuthSeedingRequirement",
    "LocalAuthSeedAction",
    "RecoveredSessionIdPersistence",
]
