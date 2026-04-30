import asyncio
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentSuccess:
    output: str


@dataclass(frozen=True)
class AgentIncomplete:
    partial_output: str


@dataclass(frozen=True)
class PreflightFailure:
    failures: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class UsageLimitHit:
    last_output: str


@dataclass(frozen=True)
class AgentTimeoutHit:
    last_output: str


AgentResult = AgentSuccess | AgentIncomplete | PreflightFailure | UsageLimitHit | AgentTimeoutHit


@dataclass
class CancellationToken:
    _event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _preserve: bool = field(default=False, init=False)

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def wants_worktree_preserved(self) -> bool:
        return self._preserve

    def cancel(self, *, preserve_worktree: bool = False) -> None:
        if self._event.is_set():
            return
        if preserve_worktree:
            self._preserve = True
        self._event.set()
