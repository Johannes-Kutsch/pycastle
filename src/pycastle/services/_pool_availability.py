from __future__ import annotations

from typing import TYPE_CHECKING

from pycastle.errors import UsageLimitError
from pycastle.services.credential_pool import CredentialPool

if TYPE_CHECKING:
    from datetime import datetime


class PoolAvailabilityHelper:
    """Pool-backed availability helper for credential-based services.

    Owns a CredentialPool and the currently-picked credential, and
    translates pool exhaustion into UsageLimitError (ADR 0049).
    Private to the services package.
    """

    def __init__(
        self,
        accounts: list[tuple[str, str]] | str,
        provider: str,
    ) -> None:
        if isinstance(accounts, str):
            accounts = [("account 1", accounts)]
        self._pool = CredentialPool(accounts)
        self._provider = provider
        self._current_token: str | None = None

    def is_available(
        self, now: datetime | None = None, *, model: str | None = None
    ) -> bool:
        if model is not None:
            return self._pool.has_available_for_model(model, now=now)
        return self._pool.has_available(now=now)

    def next_wake_time(self) -> datetime:
        return self._pool.earliest_wake_time()

    def mark_exhausted(
        self,
        reset_time: datetime | None,
        now: datetime | None = None,
    ) -> None:
        if self._current_token is not None:
            self._pool.mark_exhausted(self._current_token, reset_time, now=now)

    def mark_permanently_exhausted(self) -> str | None:
        if self._current_token is None:
            return None
        return self._pool.mark_permanently_exhausted(self._current_token)

    def mark_model_restricted(self, model: str) -> None:
        if self._current_token is not None:
            self._pool.mark_model_restricted(self._current_token, model)

    def account_names(self) -> list[str]:
        return self._pool.names()

    def pick_token(self) -> str:
        try:
            _, token = self._pool.pick()
        except RuntimeError as pick_exc:
            try:
                wake_time = self._pool.earliest_wake_time()
            except RuntimeError as wake_exc:
                raise UsageLimitError(
                    is_permanent=True, provider=self._provider
                ) from wake_exc
            raise UsageLimitError(
                reset_time=wake_time, provider=self._provider
            ) from pick_exc
        self._current_token = token
        return token
