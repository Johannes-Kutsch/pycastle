from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path

from ..agents.output_protocol import AgentRole
from ..session import SESSION_DIR_NAME, RunKind
from .agent_service import (
    AssistantTurn,
    HardError,
    ParsedTurn,
    Result,
    TransientError,
    UsageLimit,
)

_RESET_TIME_RE = re.compile(
    r"try again at\s+"
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
    match = _RESET_TIME_RE.search(message)
    if match is None:
        return None

    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    ampm = match.group("ampm").upper()
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0

    month = match.group("month")
    day = match.group("day")
    year = match.group("year")
    if month is None or day is None or year is None:
        return None

    month_num = _MONTHS.get(month.lower())
    if month_num is None:
        return None

    try:
        return datetime(
            int(year),
            month_num,
            int(day),
            hour,
            minute,
            tzinfo=timezone.utc,
        ).astimezone()
    except ValueError:
        return None


def _extract_usage_limit(event: dict[str, object]) -> UsageLimit | None:
    data = _error_data(event)
    if data is None:
        return None
    if data.get("statusCode") != 429:
        return None
    message = data.get("message")
    if not isinstance(message, str):
        return UsageLimit(reset_time=None, raw_message=None)
    reset_time = _parse_reset_time(message)
    raw_message = None if reset_time is not None else message
    return UsageLimit(reset_time=reset_time, raw_message=raw_message)


def _error_data(event: dict[str, object]) -> dict[str, object] | None:
    error = event.get("error")
    if not isinstance(error, dict):
        return None
    data = error.get("data")
    if not isinstance(data, dict):
        return None
    return data


def _extract_error(event: dict[str, object]) -> HardError | TransientError | None:
    data = _error_data(event)
    if data is None:
        return None

    message = data.get("message")
    if not isinstance(message, str) or not message:
        return None

    status = data.get("statusCode")
    if isinstance(status, int):
        if status >= 500:
            return TransientError(status_code=status, raw_message=message)
        if 400 <= status < 500:
            return HardError(status_code=status, raw_message=message)

    if status is None:
        return TransientError(status_code=None, raw_message=message)

    return None


@dataclasses.dataclass
class OpenCodeService:
    api_key: str | None = None

    @property
    def name(self) -> str:
        return "opencode"

    def build_command(
        self,
        role: AgentRole = AgentRole.IMPLEMENTER,
        model: str = "",
        effort: str = "",
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
    ) -> str:
        del role, effort
        parts = ["opencode run", "--format json"]
        if run_kind == RunKind.RESUME and session_uuid:
            parts.append(f"--session {session_uuid}")
        if model:
            parts.append(f"--model opencode-go/{model}")
        parts.append('"$(cat /tmp/.pycastle_prompt)"')
        return " ".join(parts)

    def build_env(
        self,
        state_dir_container_path: str | None = None,
        token: str | None = None,
    ) -> dict[str, str]:
        del token
        env: dict[str, str] = {"TZ": "UTC"}
        if state_dir_container_path:
            env["OPENCODE_HOME"] = state_dir_container_path
        if self.api_key:
            env["OPENCODE_GO_API_KEY"] = self.api_key
        return env

    def run(
        self,
        lines: Iterable[str],
        on_thread_id: Callable[[str], None] | None = None,
    ) -> Iterator[ParsedTurn]:
        assistant_turns: list[str] = []
        seen_session_id: str | None = None
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            session_id = event.get("sessionID")
            if (
                isinstance(session_id, str)
                and session_id
                and session_id != seen_session_id
                and on_thread_id is not None
            ):
                seen_session_id = session_id
                on_thread_id(session_id)

            if event.get("type") == "text":
                part = event.get("part")
                if not isinstance(part, dict):
                    continue
                if part.get("type") != "text":
                    continue
                time = part.get("time")
                if not isinstance(time, dict) or time.get("end") is None:
                    continue
                text = part.get("text")
                if not isinstance(text, str):
                    continue
                stripped = text.strip()
                if not stripped:
                    continue
                assistant_turns.append(stripped)
                yield AssistantTurn(text=stripped)
                continue

            if event.get("type") == "session.status":
                status = event.get("status")
                if (
                    isinstance(status, dict)
                    and status.get("type") == "idle"
                    and assistant_turns
                ):
                    yield Result(text="\n\n".join(assistant_turns))
                return

            if event.get("type") == "error":
                limit = _extract_usage_limit(event)
                if limit is not None:
                    yield limit
                else:
                    classified = _extract_error(event)
                    if classified is not None:
                        yield classified
                return

    def is_available(self, now: datetime | None = None) -> bool:
        del now
        return True

    def next_wake_time(self) -> datetime:
        return datetime.now(timezone.utc)

    def mark_exhausted(self, reset_time: datetime | None) -> None:
        del reset_time

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        if namespace:
            return f"{SESSION_DIR_NAME}/{role.value}/{namespace}/opencode/"
        return f"{SESSION_DIR_NAME}/{role.value}/opencode/"

    def is_resumable(self, state_dir: Path) -> bool:
        return (state_dir / "session_id").is_file()

    def valid_models(self) -> frozenset[str]:
        return frozenset(
            {
                "deepseek-v4-flash",
                "deepseek-v4-pro",
                "glm-5",
                "glm-5.1",
                "hy3-preview",
                "kimi-k2.5",
                "kimi-k2.6",
                "mimo-v2-omni",
                "mimo-v2-pro",
                "mimo-v2.5",
                "mimo-v2.5-pro",
                "minimax-m2.5",
                "minimax-m2.7",
                "qwen3.5-plus",
                "qwen3.6-plus",
                "qwen3.7-max",
            }
        )

    def valid_efforts(self) -> frozenset[str]:
        return frozenset({"medium"})
