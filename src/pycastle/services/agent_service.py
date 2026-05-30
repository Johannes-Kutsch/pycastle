from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ..agents.output_protocol import AgentRole
from ..session import RunKind
from .provider_session_state import ProviderSessionState, ProviderSessionStateRequest


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


@dataclasses.dataclass
class HardError:
    status_code: int
    raw_message: str


ParsedTurn = (
    AssistantTurn
    | PromptTokens
    | UnsupportedTokens
    | Result
    | UsageLimit
    | TransientError
    | HardError
)


class AgentService(Protocol):
    @property
    def name(self) -> str: ...

    def build_command(
        self,
        role: AgentRole,
        model: str,
        effort: str,
        run_kind: RunKind,
        session_uuid: str | None,
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

    def mark_exhausted(self, reset_time: datetime | None) -> None: ...

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None: ...

    def is_resumable(self, state_dir: Path) -> bool: ...

    def provider_session_state(
        self, request: ProviderSessionStateRequest
    ) -> ProviderSessionState: ...

    def valid_models(self) -> frozenset[str]: ...

    def valid_efforts(self) -> frozenset[str]: ...
