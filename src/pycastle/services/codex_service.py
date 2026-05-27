from __future__ import annotations

import dataclasses
import json
import logging
import re
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import _time as _time_module
from ..agents.output_protocol import AgentRole
from ..session import SESSION_DIR_NAME, RunKind
from .agent_service import (
    AssistantTurn,
    HardError,
    ParsedTurn,
    Tokens,
    TransientError,
    UsageLimit,
)
from ._wake_time import compute_wake_time

_log = logging.getLogger(__name__)

_USAGE_LIMIT_SUBSTRING = "You've hit your usage limit"

# Matches "try again at 3:30 PM" (same-day) or "try again at March 15th, 2026 3:30 PM" (cross-day)
_RESET_TIME_RE = re.compile(
    r"(?:or\s+)?try again at\s+"
    r"(?:(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,\s+(?P<year>\d{4})\s+)?"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>AM|PM)",
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
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

_UNAUTHORIZED_RE = re.compile(
    r"\b(?:401|unauthorized|missing bearer|basic authentication)\b",
    re.IGNORECASE,
)
_HTTP_STATUS_RE = re.compile(r"\bstatus\s+(?P<status>\d{3})\b", re.IGNORECASE)


def _classify_error_message(message: str) -> HardError | TransientError | None:
    if _UNAUTHORIZED_RE.search(message):
        return HardError(status_code=401, raw_message=message)

    match = _HTTP_STATUS_RE.search(message)
    if match is None:
        return None

    status = int(match.group("status"))
    if status >= 500:
        return TransientError(status_code=status, raw_message=message)
    if 400 <= status < 500:
        return HardError(status_code=status, raw_message=message)
    return None


def _parse_reset_time(message: str) -> datetime | None:
    """Extract a UTC reset datetime from a Codex usage-limit error message."""
    match = _RESET_TIME_RE.search(message)
    if not match:
        return None

    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    ampm = match.group("ampm").upper()
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0

    year_str = match.group("year")
    month_str = match.group("month")
    day_str = match.group("day")

    if year_str and month_str and day_str:
        month = _MONTHS.get(month_str.lower())
        if month is None:
            return None
        try:
            return datetime(int(year_str), month, int(day_str), hour, minute, tzinfo=timezone.utc)
        except ValueError:
            return None

    now = datetime.now(timezone.utc)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate < now - timedelta(minutes=2):
        candidate += timedelta(days=1)
    return candidate


def _extract_usage_limit(message: str) -> UsageLimit | None:
    """Return a UsageLimit if message contains the usage-limit substring."""
    if _USAGE_LIMIT_SUBSTRING not in message:
        return None
    reset_time = _parse_reset_time(message)
    raw = message if reset_time is None else None
    return UsageLimit(reset_time=reset_time, raw_message=raw)


def _usage_value(usage: dict, *names: str) -> int:
    for name in names:
        value = usage.get(name)
        if isinstance(value, int):
            return value
    return 0


@dataclasses.dataclass
class CodexService:
    _exhausted_until: datetime | None = dataclasses.field(default=None, init=False)

    @property
    def name(self) -> str:
        return "codex"

    def is_available(self, now: datetime | None = None) -> bool:
        if self._exhausted_until is None:
            return True
        now = now or _time_module.now_local()
        return now >= self._exhausted_until

    def next_wake_time(self) -> datetime:
        if self._exhausted_until is None:
            raise RuntimeError("CodexService.next_wake_time called when not exhausted")
        return self._exhausted_until

    def mark_exhausted(
        self, reset_time: datetime | None, *, _now: datetime | None = None
    ) -> None:
        now = _now or _time_module.now_local()
        wake, _ = compute_wake_time(reset_time, now)
        if wake.tzinfo is None:
            wake = wake.replace(tzinfo=timezone.utc)
        self._exhausted_until = wake

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        if namespace:
            return f"{SESSION_DIR_NAME}/{role.value}/{namespace}/codex/"
        return f"{SESSION_DIR_NAME}/{role.value}/codex/"

    def is_resumable(self, state_dir: Path) -> bool:
        sessions_dir = state_dir / "sessions"
        if not sessions_dir.is_dir():
            return False
        return any(sessions_dir.glob("rollout-*.jsonl"))

    def valid_efforts(self) -> frozenset[str]:
        return frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})

    def build_command(
        self,
        role: AgentRole = AgentRole.IMPLEMENTER,
        model: str = "",
        effort: str = "",
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
    ) -> str:
        if run_kind == RunKind.RESUME and session_uuid:
            parts = [f"codex exec resume {session_uuid}"]
        else:
            parts = ["codex exec"]
        if model:
            parts.append(f"-m {model}")
        if effort:
            parts.append(f"-c model_reasoning_effort={effort}")
        parts.append("-c approval_policy=never")
        if run_kind != RunKind.RESUME:
            parts.append("--sandbox danger-full-access")
        parts += [
            "--json",
            "< /tmp/.pycastle_prompt",
        ]
        return " ".join(parts)

    def build_env(
        self,
        state_dir_container_path: str | None = None,
        token: str | None = None,
    ) -> dict[str, str]:
        env: dict[str, str] = {"TZ": "UTC"}
        if state_dir_container_path:
            env["CODEX_HOME"] = state_dir_container_path
        return env

    def run(
        self,
        lines: Iterable[str],
        on_thread_id: Callable[[str], None] | None = None,
    ) -> Iterator[ParsedTurn]:
        for line in lines:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue

            event_type = obj.get("type")

            if event_type == "thread.started":
                thread_id = obj.get("thread_id")
                if thread_id and on_thread_id is not None:
                    on_thread_id(thread_id)
                continue

            if event_type == "item.completed":
                item = obj.get("item") or {}
                item_type = item.get("type")
                if item_type == "agent_message":
                    content = item.get("text")
                    if content is None:
                        content = item.get("content") or ""
                    if content:
                        yield AssistantTurn(text=content)
                continue

            if event_type == "turn.completed":
                usage = obj.get("usage") or {}
                count = (
                    _usage_value(usage, "input_tokens")
                    + _usage_value(usage, "cached_input_tokens", "cached_tokens")
                    + _usage_value(usage, "output_tokens")
                    + _usage_value(
                        usage, "reasoning_output_tokens", "reasoning_tokens"
                    )
                )
                yield Tokens(count=count)
                return

            if event_type == "turn.failed":
                error = obj.get("error") or {}
                message = error.get("message") or ""
                limit = _extract_usage_limit(message)
                if limit is not None:
                    yield limit
                else:
                    classified = _classify_error_message(message)
                    if classified is not None:
                        yield classified
                    _log.warning("codex turn.failed: %s", message)
                return

            if event_type == "error":
                message = obj.get("message") or ""
                limit = _extract_usage_limit(message)
                if limit is not None:
                    yield limit
                else:
                    classified = _classify_error_message(message)
                    if classified is not None:
                        yield classified
                    _log.warning("codex error: %s", message)
                return
