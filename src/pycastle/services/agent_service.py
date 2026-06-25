from __future__ import annotations

import dataclasses
import enum
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ..agents.output_protocol import AgentRole

if TYPE_CHECKING:
    from ..runtime_session import (
        ProviderSessionPreferences,
        ProviderSessionPreferencesRequest,
        ProviderSessionState,
        ProviderSessionStateRequest,
    )


@dataclasses.dataclass
class AssistantTurn:
    text: str


@dataclasses.dataclass
class PromptTokens:
    count: int


@dataclasses.dataclass
class UnsupportedTokens:
    count: int
    source: str


@dataclasses.dataclass
class Result:
    text: str


@dataclasses.dataclass
class UsageLimit:
    reset_time: datetime | None
    raw_message: str | None = None
    is_permanent: bool = False


@dataclasses.dataclass
class TransientError:
    status_code: int | None
    raw_message: str
    observations: tuple[Any, ...] = dataclasses.field(
        default=(),
        compare=False,
    )


@dataclasses.dataclass
class HardError:
    status_code: int
    raw_message: str
    classification: str | None = None
    observations: tuple[Any, ...] = dataclasses.field(
        default=(),
        compare=False,
    )


@dataclasses.dataclass
class CredentialFailure:
    raw_message: str
    service_name: str
    source_observations: tuple[Any, ...] = dataclasses.field(compare=False)
    status_code: int | None = None
    classification: str | None = None


ParsedTurn = (
    AssistantTurn
    | PromptTokens
    | UnsupportedTokens
    | Result
    | UsageLimit
    | TransientError
    | HardError
    | CredentialFailure
)


class ToolPolicy(enum.Enum):
    RESTRICTED = "restricted"
    PARTIAL = "partial"
    FULL = "full"


class ProviderStatePreparationAction(Protocol):
    def apply(self) -> None: ...


class ProviderSessionRecordingStore(Protocol):
    def save_service_session_id(self, service_name: str, session_id: str) -> None: ...


class AgentService(Protocol):
    @property
    def name(self) -> str: ...

    def build_command(
        self,
        role: AgentRole,
        model: str,
        effort: str,
        run_kind: Any,
        session_uuid: str | None,
        *,
        tool_policy: Any | None = None,
    ) -> str: ...

    def build_env(
        self,
        state_dir_container_path: str | None = None,
        token: str | None = None,
    ) -> dict[str, str]: ...

    def run(
        self,
        lines: Iterable[str],
        on_provider_session_id: Callable[[str], None] | None = None,
    ) -> Iterator[ParsedTurn]: ...

    def is_available(self, now: datetime | None = None) -> bool: ...

    def next_wake_time(self) -> datetime: ...

    def mark_exhausted(
        self,
        reset_time: datetime | None,
    ) -> None: ...

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None: ...

    def is_resumable(self, state_dir: Path) -> bool: ...

    def valid_models(self) -> frozenset[str]: ...

    def valid_efforts(self) -> frozenset[str]: ...

    def provider_session_preferences(
        self,
        request: "ProviderSessionPreferencesRequest",
    ) -> "ProviderSessionPreferences": ...

    def provider_session_state(
        self,
        request: "ProviderSessionStateRequest",
    ) -> "ProviderSessionState": ...


__all__ = [
    "AgentService",
    "AssistantTurn",
    "CredentialFailure",
    "HardError",
    "ParsedTurn",
    "PromptTokens",
    "ProviderSessionRecordingStore",
    "ProviderStatePreparationAction",
    "Result",
    "ToolPolicy",
    "TransientError",
    "UnsupportedTokens",
    "UsageLimit",
]
