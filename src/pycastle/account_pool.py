from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta


@dataclasses.dataclass
class _Account:
    name: str
    token: str
    exhausted_until: datetime | None = None


class AccountPool:
    """In-memory ordered registry of Claude accounts with usage-limit failover.

    Accounts are stored in priority order (highest first). `pick()` returns the
    first non-exhausted account; `mark_exhausted` records a per-token wake-time
    so the orchestrator can sleep only when *every* account is exhausted.
    """

    def __init__(self, accounts: list[tuple[str, str]]) -> None:
        if not accounts:
            raise ValueError("AccountPool requires at least one account")
        self._accounts: list[_Account] = [
            _Account(name=n, token=t) for n, t in accounts
        ]

    def _is_exhausted(self, acc: _Account, now: datetime) -> bool:
        return acc.exhausted_until is not None and acc.exhausted_until > now

    def pick(self, now: datetime | None = None) -> tuple[str, str]:
        now = now or datetime.now()
        for acc in self._accounts:
            if not self._is_exhausted(acc, now):
                return acc.name, acc.token
        raise RuntimeError("AccountPool.pick called with no available accounts")

    def mark_exhausted(
        self, token: str, reset_time: datetime | None, now: datetime | None = None
    ) -> None:
        now = now or datetime.now()
        if reset_time is not None:
            wake = reset_time + timedelta(minutes=2)
        else:
            next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(
                hours=1
            )
            wake = next_hour + timedelta(minutes=2)
        for acc in self._accounts:
            if acc.token == token:
                acc.exhausted_until = wake
                return

    def has_available(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        return any(not self._is_exhausted(a, now) for a in self._accounts)

    def earliest_wake_time(self) -> datetime:
        wakes = [
            a.exhausted_until for a in self._accounts if a.exhausted_until is not None
        ]
        if not wakes:
            raise RuntimeError(
                "AccountPool.earliest_wake_time called with no exhausted accounts"
            )
        return min(wakes)

    def names(self) -> list[str]:
        return [a.name for a in self._accounts]
