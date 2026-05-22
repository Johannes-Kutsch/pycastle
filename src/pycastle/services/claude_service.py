from __future__ import annotations

import dataclasses
import json
import re
import shlex
from collections.abc import Iterable, Iterator
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Literal

from ..agents.output_protocol import AgentRole
from .. import _time as _time_module
from ..session import SESSION_DIR_NAME, RunKind
from .flag_profiles import flag_profile_for
from .agent_service import (
    AssistantTurn,
    HardError,
    ParsedTurn,
    Result,
    Tokens,
    TransientError,
    UsageLimit,
)
from ._wake_time import compute_wake_time


# ── private account pool ──────────────────────────────────────────────────────


@dataclasses.dataclass
class _Account:
    name: str
    token: str
    exhausted_until: datetime | None = None


class _AccountPool:
    def __init__(self, accounts: list[tuple[str, str]]) -> None:
        if not accounts:
            raise ValueError("ClaudeService requires at least one account")
        self._accounts: list[_Account] = [
            _Account(name=n, token=t) for n, t in accounts
        ]

    def _is_exhausted(self, acc: _Account, now: datetime) -> bool:
        return acc.exhausted_until is not None and acc.exhausted_until > now

    def pick(self, now: datetime | None = None) -> tuple[str, str]:
        now = now or _time_module.now_local()
        for acc in self._accounts:
            if not self._is_exhausted(acc, now):
                return acc.name, acc.token
        raise RuntimeError("No available Claude accounts")

    def mark_exhausted(
        self, token: str, reset_time: datetime | None, now: datetime | None = None
    ) -> None:
        now = now or _time_module.now_local()
        wake, _ = compute_wake_time(reset_time, now)
        for acc in self._accounts:
            if acc.token == token:
                acc.exhausted_until = wake
                return

    def has_available(self, now: datetime | None = None) -> bool:
        now = now or _time_module.now_local()
        return any(not self._is_exhausted(a, now) for a in self._accounts)

    def earliest_wake_time(self) -> datetime:
        wakes = [
            a.exhausted_until for a in self._accounts if a.exhausted_until is not None
        ]
        if not wakes:
            raise RuntimeError("No exhausted accounts")
        return min(wakes)

    def names(self) -> list[str]:
        return [a.name for a in self._accounts]


_RESET_TIME_RE = re.compile(
    r"resets\s+"
    r"(?:(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s+)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?(?P<ampm>am|pm)\s+\(UTC\)",
    re.IGNORECASE,
)

_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


def _check_api_error(line: str) -> "TransientError | HardError | Literal[False]":
    """Classify non-429 is_error: true result envelopes into transient or hard error.

    Returns False for 429 (handled by _check_usage_limit) and for non-error lines.
    """
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return False
    if not isinstance(obj, dict) or not obj.get("is_error"):
        return False
    if obj.get("type") != "result":
        return False
    status = obj.get("api_error_status")
    # 429 is handled unchanged by _check_usage_limit
    if status == 429:
        return False
    if status is None or (isinstance(status, int) and status >= 500):
        return TransientError(
            status_code=status if isinstance(status, int) else None,
            raw_message=line,
        )
    if isinstance(status, int) and 400 <= status < 500:
        return HardError(status_code=status, raw_message=line)
    return False


def _check_usage_limit(line: str) -> datetime | None | Literal[False]:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return False
    if not isinstance(obj, dict) or obj.get("api_error_status") != 429:
        return False
    result_text = obj.get("result")
    if not isinstance(result_text, str):
        return None
    match = _RESET_TIME_RE.search(result_text)
    if not match:
        return None

    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    ampm = match.group("ampm").lower()
    if not (1 <= hour <= 12) or not (0 <= minute <= 59):
        return None
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0

    now_l = _time_module.now_local()
    now_utc = now_l.astimezone(timezone.utc)

    month_str = match.group("month")
    if month_str is not None:
        month = _MONTHS.get(month_str.lower())
        if month is None:
            return None
        day = int(match.group("day"))
        try:
            utc_dt = datetime(
                now_utc.year, month, day, hour, minute, tzinfo=timezone.utc
            )
        except ValueError:
            return None
        local_dt = utc_dt.astimezone()
        if local_dt < now_l - timedelta(days=31):
            try:
                utc_dt = utc_dt.replace(year=utc_dt.year + 1)
            except ValueError:
                return None
            local_dt = utc_dt.astimezone()
        return local_dt

    utc_dt = datetime.combine(now_utc.date(), time(hour, minute), tzinfo=timezone.utc)
    local_dt = utc_dt.astimezone()
    if local_dt < now_l - timedelta(minutes=2):
        local_dt += timedelta(days=1)
    return local_dt


def _extract_turn(line: str) -> tuple[str | None, int | None]:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return None, None
    msg = obj.get("message") or {}
    content = msg.get("content") or []
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = (block.get("text") or "").strip()
            if text:
                parts.append(text)
    turn_text = "\n\n".join(parts) if parts else None

    usage = msg.get("usage") or {}
    tokens: int | None = None
    if usage:
        total = (
            (usage.get("input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0)
        )
        if total > 0:
            tokens = total

    return turn_text, tokens


class ClaudeService:
    def __init__(self, accounts: list[tuple[str, str]] | None = None) -> None:
        self._pool: _AccountPool | None = (
            _AccountPool(accounts) if accounts is not None else None
        )
        self._current_token: str | None = None

    @property
    def name(self) -> str:
        return "claude"

    def is_available(self, now: datetime | None = None) -> bool:
        if self._pool is None:
            return True
        return self._pool.has_available(now=now)

    def next_wake_time(self) -> datetime:
        if self._pool is None:
            raise RuntimeError("ClaudeService.next_wake_time called with no pool")
        return self._pool.earliest_wake_time()

    def mark_exhausted(
        self, reset_time: datetime | None, *, _now: datetime | None = None
    ) -> None:
        if self._pool is not None and self._current_token is not None:
            self._pool.mark_exhausted(self._current_token, reset_time, now=_now)

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        if namespace:
            return f"{SESSION_DIR_NAME}/{role.value}/{namespace}/claude/"
        return f"{SESSION_DIR_NAME}/{role.value}/claude/"

    def is_resumable(self, state_dir: Path) -> bool:
        return state_dir.is_dir() and any(f.is_file() for f in state_dir.rglob("*"))

    def account_names(self) -> list[str]:
        if self._pool is None:
            return []
        return self._pool.names()

    def valid_efforts(self) -> frozenset[str]:
        return frozenset({"low", "medium", "high", "xhigh", "max"})

    def build_command(
        self,
        role: AgentRole = AgentRole.IMPLEMENTER,
        model: str = "",
        effort: str = "",
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
    ) -> str:
        _profile = flag_profile_for(role)
        flags = (
            "--verbose --dangerously-skip-permissions --output-format stream-json -p -"
        )
        if _profile.disallowed_tools:
            flags += f' --disallowedTools "{" ".join(_profile.disallowed_tools)}"'
        if model:
            flags += f" --model {model}"
        if effort:
            flags += f" --effort {effort}"
        if session_uuid:
            if run_kind == RunKind.RESUME:
                flags += f" --resume {shlex.quote(session_uuid)}"
            else:
                flags += f" --session-id {shlex.quote(session_uuid)}"
        return f"claude {flags} < /tmp/.pycastle_prompt"

    def build_env(
        self,
        state_dir_container_path: str | None = None,
        token: str | None = None,
    ) -> dict[str, str]:
        if token is None and self._pool is not None:
            _, self._current_token = self._pool.pick()
            token = self._current_token
        elif token is not None:
            self._current_token = token
        env: dict[str, str] = {}
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        if state_dir_container_path:
            env["CLAUDE_CONFIG_DIR"] = state_dir_container_path
        return env

    def run(self, lines: Iterable[str]) -> Iterator[ParsedTurn]:
        for line in lines:
            api_error = _check_api_error(line)
            if api_error is not False:
                yield api_error
                return
            usage_limit = _check_usage_limit(line)
            if usage_limit is not False:
                raw = line if usage_limit is None else None
                yield UsageLimit(reset_time=usage_limit, raw_message=raw)
                return
            turn, tokens = _extract_turn(line)
            if tokens is not None:
                yield Tokens(count=tokens)
            if turn is not None:
                yield AssistantTurn(text=turn)
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("type") == "result":
                r = obj.get("result")
                if isinstance(r, str):
                    yield Result(text=r)
                    return
