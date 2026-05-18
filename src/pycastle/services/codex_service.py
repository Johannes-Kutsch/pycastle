from __future__ import annotations

import dataclasses
import json
import logging
import re
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timedelta
from pathlib import Path

from ..agents.output_protocol import AgentRole
from ..session import SESSION_DIR_NAME, RunKind
from .agent_service import AssistantTurn, ParsedTurn, Tokens, UsageLimit

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
            return datetime(int(year_str), month, int(day_str), hour, minute)
        except ValueError:
            return None

    # Same-day: TZ=UTC in container makes "now" UTC-by-construction
    now = datetime.utcnow()
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate < now - timedelta(minutes=2):
        candidate += timedelta(days=1)
    return candidate


def _extract_usage_limit(message: str) -> UsageLimit | None:
    """Return a UsageLimit if message contains the usage-limit substring."""
    if _USAGE_LIMIT_SUBSTRING not in message:
        return None
    return UsageLimit(reset_time=_parse_reset_time(message))


@dataclasses.dataclass
class CodexService:
    _exhausted_until: datetime | None = dataclasses.field(default=None, init=False)

    @property
    def name(self) -> str:
        return "codex"

    def is_available(self, now: datetime | None = None) -> bool:
        if self._exhausted_until is None:
            return True
        now = now or datetime.utcnow()
        return now >= self._exhausted_until

    def next_wake_time(self) -> datetime:
        if self._exhausted_until is None:
            raise RuntimeError("CodexService.next_wake_time called when not exhausted")
        return self._exhausted_until

    def mark_exhausted(
        self, reset_time: datetime | None, *, _now: datetime | None = None
    ) -> None:
        now = _now or datetime.utcnow()
        if reset_time is not None:
            self._exhausted_until = reset_time + timedelta(minutes=2)
        else:
            next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(
                hours=1
            )
            self._exhausted_until = next_hour + timedelta(minutes=2)

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
        parts += [
            "-c approval_policy=never",
            "--sandbox danger-full-access",
            "--json",
            "< /tmp/.pycastle_prompt",
        ]
        return " ".join(parts)

    def build_env(
        self,
        state_dir_container_path: str | None = None,
        token: str | None = None,
    ) -> dict[str, str]:
        return {"TZ": "UTC", "CODEX_HOME": "/home/agent/.codex"}

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
                    content = item.get("content") or ""
                    if content:
                        yield AssistantTurn(text=content)
                continue

            if event_type == "turn.completed":
                usage = obj.get("usage") or {}
                count = (
                    (usage.get("input_tokens") or 0)
                    + (usage.get("cached_tokens") or 0)
                    + (usage.get("output_tokens") or 0)
                    + (usage.get("reasoning_tokens") or 0)
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
                    _log.warning("codex turn.failed: %s", message)
                return

            if event_type == "error":
                message = obj.get("message") or ""
                limit = _extract_usage_limit(message)
                if limit is not None:
                    yield limit
                else:
                    _log.warning("codex error: %s", message)
                return
