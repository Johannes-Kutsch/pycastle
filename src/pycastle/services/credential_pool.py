from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from .. import _time as _time_module
from ._wake_time import compute_wake_time


PERMANENT_EXHAUSTION_WAKE = datetime(9999, 12, 31, 23, 59, tzinfo=timezone.utc)


@dataclasses.dataclass
class _CredentialSlot:
    name: str
    token: str
    exhausted_until: datetime | None = None
    restricted_models: set[str] = dataclasses.field(default_factory=set)


class CredentialPool:
    def __init__(
        self,
        accounts: list[tuple[str, str]],
        *,
        empty_error_message: str = "CredentialPool requires at least one credential",
        unavailable_error_message: str = "No available credentials",
    ) -> None:
        if not accounts:
            raise ValueError(empty_error_message)
        self._accounts: list[_CredentialSlot] = [
            _CredentialSlot(name=n, token=t) for n, t in accounts
        ]
        self._unavailable_error_message = unavailable_error_message

    def _is_exhausted(self, acc: _CredentialSlot, now: datetime) -> bool:
        return acc.exhausted_until is not None and acc.exhausted_until > now

    def pick(self, now: datetime | None = None) -> tuple[str, str]:
        now = now or _time_module.now_local()
        for acc in self._accounts:
            if not self._is_exhausted(acc, now):
                return acc.name, acc.token
        raise RuntimeError(self._unavailable_error_message)

    def mark_exhausted(
        self,
        token: str,
        reset_time: datetime | None,
        now: datetime | None = None,
    ) -> None:
        now = now or _time_module.now_local()
        wake, _ = compute_wake_time(
            reset_time,
            now,
        )
        for acc in self._accounts:
            if acc.token == token:
                acc.exhausted_until = wake
                return

    def mark_permanently_exhausted(self, token: str) -> str | None:
        for acc in self._accounts:
            if acc.token == token:
                acc.exhausted_until = PERMANENT_EXHAUSTION_WAKE
                return acc.name
        return None

    def has_available(self, now: datetime | None = None) -> bool:
        now = now or _time_module.now_local()
        return any(not self._is_exhausted(a, now) for a in self._accounts)

    def earliest_wake_time(self) -> datetime:
        wakes = [
            a.exhausted_until
            for a in self._accounts
            if a.exhausted_until is not None
            and a.exhausted_until < PERMANENT_EXHAUSTION_WAKE
        ]
        if not wakes:
            raise RuntimeError("No exhausted accounts with finite wake time")
        return min(wakes)

    def mark_model_restricted(self, token: str, model: str) -> None:
        for acc in self._accounts:
            if acc.token == token:
                acc.restricted_models.add(model)
                return

    def has_available_for_model(self, model: str, now: datetime | None = None) -> bool:
        now = now or _time_module.now_local()
        return any(
            not self._is_exhausted(a, now) and model not in a.restricted_models
            for a in self._accounts
        )

    def pick_for_model(
        self, model: str, now: datetime | None = None
    ) -> tuple[str, str]:
        now = now or _time_module.now_local()
        for acc in self._accounts:
            if not self._is_exhausted(acc, now) and model not in acc.restricted_models:
                return acc.name, acc.token
        raise RuntimeError(self._unavailable_error_message)

    def names(self) -> list[str]:
        return [a.name for a in self._accounts]
