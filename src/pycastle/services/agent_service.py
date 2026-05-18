from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ..agents.output_protocol import AgentRole
from ..session_resume import RunKind


@dataclasses.dataclass
class AssistantTurn:
    text: str


@dataclasses.dataclass
class Tokens:
    count: int


@dataclasses.dataclass
class Result:
    text: str


@dataclasses.dataclass
class UsageLimit:
    reset_time: datetime | None


ParsedTurn = AssistantTurn | Tokens | Result | UsageLimit


class AgentService(Protocol):
    @property
    def name(self) -> str: ...

    def build_command(
        self,
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

    def run(self, lines: Iterable[str]) -> Iterator[ParsedTurn]: ...

    def is_available(self, now: datetime | None = None) -> bool: ...

    def next_wake_time(self) -> datetime: ...

    def mark_exhausted(self, reset_time: datetime | None) -> None: ...

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None: ...

    def is_resumable(self, state_dir: Path) -> bool: ...
