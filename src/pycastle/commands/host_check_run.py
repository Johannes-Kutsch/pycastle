from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


@dataclass(frozen=True)
class HostCheckFailure:
    name: str
    command: str
    output: str


@dataclass(frozen=True)
class HostCheckRunPassed:
    checked_sha: str


@dataclass(frozen=True)
class HostCheckRunFailed:
    checked_sha: str
    failures: tuple[HostCheckFailure, ...]
    issue_numbers: tuple[int, ...]


HostCheckRunOutcome: TypeAlias = HostCheckRunPassed | HostCheckRunFailed
