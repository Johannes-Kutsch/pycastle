import dataclasses
import enum
import json
import re
from collections.abc import Callable, Iterable
from datetime import datetime, time, timedelta, timezone
from typing import Literal, TypeAlias

from .errors import UsageLimitError

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


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class AgentRole(enum.Enum):
    PLANNER = "planner"
    PREFLIGHT_ISSUE = "preflight_issue"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    MERGER = "merger"
    IMPROVE = "improve"


@dataclasses.dataclass(frozen=True)
class PlannerOutput:
    issues: list[dict]


@dataclasses.dataclass(frozen=True)
class IssueOutput:
    labels: list[str]
    number: int


@dataclasses.dataclass(frozen=True)
class CompletionOutput:
    # Bare-integer <issue>N</issue> tags captured from the COMPLETE turn.
    # Phase 02 (PRD) emits exactly one; phase 03 emits one per sub-issue (ignored
    # by improve_phase — only phase 02's first number is used as prd_number).
    issue_numbers: tuple[int, ...] = ()


@dataclasses.dataclass(frozen=True)
class NoCandidateOutput:
    pass


@dataclasses.dataclass(frozen=True)
class CommitMessageOutput:
    message: str | None


AgentOutput: TypeAlias = (
    PlannerOutput
    | IssueOutput
    | CompletionOutput
    | NoCandidateOutput
    | CommitMessageOutput
)


class AgentOutputProtocolError(Exception):
    pass


class PlanParseError(AgentOutputProtocolError):
    pass


class IssueParseError(AgentOutputProtocolError):
    pass


class PromiseParseError(AgentOutputProtocolError):
    pass


def _extract_planner_output(text: str) -> PlannerOutput:
    match = re.search(r"<plan>([\s\S]*?)</plan>", text)
    if not match:
        raise PlanParseError("Planner produced no <plan> tag.")
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise PlanParseError(
            f"Planner produced malformed JSON inside <plan> tag: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise PlanParseError(f"Plan JSON must be an object, got {type(data).__name__}.")
    # Accept "unblocked_issues" because the LLM inconsistently uses that name;
    # "issues" is canonical.
    if "unblocked_issues" in data:
        raw = data["unblocked_issues"]
    elif "issues" in data:
        raw = data["issues"]
    else:
        raise PlanParseError(
            f"Plan JSON has no 'unblocked_issues' or 'issues' key. Keys found: {list(data.keys())}"
        )
    try:
        return PlannerOutput(
            issues=[{"number": i["number"], "title": i["title"]} for i in raw]
        )
    except (KeyError, TypeError) as exc:
        raise PlanParseError(
            f"Plan JSON issues list has unexpected structure: {exc}"
        ) from exc


def _extract_issue_output(text: str) -> IssueOutput:
    match = re.search(r"<issue>([\s\S]*?)</issue>", text)
    if not match:
        raise IssueParseError("Agent produced no <issue>...</issue> tag.")
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise IssueParseError(f"Malformed JSON inside <issue> tag: {exc}") from exc
    try:
        number = int(data["number"])
        labels = [str(label) for label in data["labels"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise IssueParseError(f"<issue> JSON has unexpected structure: {exc}") from exc
    return IssueOutput(labels=labels, number=number)


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

    now_utc = _now_utc()
    now_local = now_utc.astimezone().replace(tzinfo=None)

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
        local_dt = utc_dt.astimezone().replace(tzinfo=None)
        if local_dt < now_local - timedelta(days=31):
            try:
                utc_dt = utc_dt.replace(year=utc_dt.year + 1)
            except ValueError:
                return None
            local_dt = utc_dt.astimezone().replace(tzinfo=None)
        return local_dt

    utc_dt = datetime.combine(now_utc.date(), time(hour, minute), tzinfo=timezone.utc)
    local_dt = utc_dt.astimezone().replace(tzinfo=None)
    if local_dt < now_local - timedelta(minutes=2):
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


_COMMIT_MESSAGE_RE = re.compile(r"<commit_message>([\s\S]*?)</commit_message>")
_ISSUE_NUMBER_RE = re.compile(r"<issue>(\d+)</issue>")


def _extract_issue_numbers(text: str) -> tuple[int, ...]:
    return tuple(int(m.group(1)) for m in _ISSUE_NUMBER_RE.finditer(text))


def process_stream(
    lines: Iterable[str],
    on_turn: Callable[[str], None],
    role: AgentRole,
    on_tokens: Callable[[int], None] | None = None,
) -> AgentOutput:
    collected: list[str] = []
    result_text: str | None = None
    for line in lines:
        collected.append(line)
        usage_limit = _check_usage_limit(line)
        if usage_limit is not False:
            raise UsageLimitError(reset_time=usage_limit)
        turn, tokens = _extract_turn(line)
        if tokens is not None and on_tokens is not None:
            on_tokens(tokens)
        if turn is not None:
            on_turn(turn)
            if role in (AgentRole.IMPLEMENTER, AgentRole.REVIEWER):
                match = _COMMIT_MESSAGE_RE.search(turn)
                if match:
                    return CommitMessageOutput(message=match.group(1).strip())
            elif role == AgentRole.IMPROVE:
                if re.search(r"<promise>NO-CANDIDATE</promise>", turn):
                    return NoCandidateOutput()
                if re.search(r"<promise>COMPLETE</promise>", turn):
                    return CompletionOutput(issue_numbers=_extract_issue_numbers(turn))
            elif role == AgentRole.MERGER:
                if re.search(r"<promise>COMPLETE</promise>", turn):
                    return CompletionOutput()
            elif role == AgentRole.PLANNER:
                try:
                    return _extract_planner_output(turn)
                except PlanParseError:
                    pass
            elif role == AgentRole.PREFLIGHT_ISSUE:
                try:
                    return _extract_issue_output(turn)
                except IssueParseError:
                    pass
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            r = obj.get("result")
            if isinstance(r, str):
                result_text = r
                break
    text = result_text if result_text is not None else "\n".join(collected)
    tail = f"\nOutput tail: {text[-300:]!r}"
    if role == AgentRole.PREFLIGHT_ISSUE:
        try:
            return _extract_issue_output(text)
        except IssueParseError as exc:
            raise IssueParseError(f"{exc}{tail}") from exc.__cause__
    if role == AgentRole.PLANNER:
        try:
            return _extract_planner_output(text)
        except PlanParseError as exc:
            raise PlanParseError(f"{exc}{tail}") from exc.__cause__
    if role in (AgentRole.IMPLEMENTER, AgentRole.REVIEWER):
        match = _COMMIT_MESSAGE_RE.search(text)
        if not match:
            return CommitMessageOutput(message=None)
        return CommitMessageOutput(message=match.group(1).strip())
    if role == AgentRole.IMPROVE:
        if re.search(r"<promise>NO-CANDIDATE</promise>", text):
            return NoCandidateOutput()
        if not re.search(r"<promise>COMPLETE</promise>", text):
            raise PromiseParseError(
                f"Agent produced no <promise>COMPLETE</promise> or"
                f" <promise>NO-CANDIDATE</promise> tag.{tail}"
            )
        return CompletionOutput(issue_numbers=_extract_issue_numbers(text))
    if not re.search(r"<promise>COMPLETE</promise>", text):
        raise PromiseParseError(
            f"Agent produced no <promise>COMPLETE</promise> tag.{tail}"
        )
    return CompletionOutput()
