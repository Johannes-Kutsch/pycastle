import dataclasses
import enum
import json
import re
from collections.abc import Callable, Iterable
from typing import TypeAlias

from .errors import UsageLimitError


class AgentRole(enum.Enum):
    PLANNER = "planner"
    PREFLIGHT_ISSUE = "preflight_issue"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    MERGER = "merger"


@dataclasses.dataclass(frozen=True)
class PlannerOutput:
    issues: list[dict]


@dataclasses.dataclass(frozen=True)
class IssueOutput:
    labels: list[str]
    number: int


@dataclasses.dataclass(frozen=True)
class CompletionOutput:
    pass


AgentOutput: TypeAlias = PlannerOutput | IssueOutput | CompletionOutput


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


def _is_usage_limit_line(line: str, patterns: tuple[str, ...]) -> bool:
    try:
        obj = json.loads(line)
        if isinstance(obj, dict):
            if obj.get("type") == "result" and obj.get("is_error"):
                if obj.get("api_error_status") == 429:
                    return True
                result_text = obj.get("result")
                if isinstance(result_text, str) and any(
                    p.lower() in result_text.lower() for p in patterns
                ):
                    return True
            return False
    except json.JSONDecodeError:
        pass
    line_lower = line.lower()
    return any(p.lower() in line_lower for p in patterns)


def _extract_turn(line: str) -> str | None:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return None
    content = (obj.get("message") or {}).get("content") or []
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = (block.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts) if parts else None


def process_stream(
    lines: Iterable[str],
    on_turn: Callable[[str], None],
    role: AgentRole,
    usage_limit_patterns: tuple[str, ...],
) -> AgentOutput:
    collected: list[str] = []
    result_text: str | None = None
    for line in lines:
        collected.append(line)
        if _is_usage_limit_line(line, usage_limit_patterns):
            raise UsageLimitError(line)
        turn = _extract_turn(line)
        if turn is not None:
            on_turn(turn)
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            r = obj.get("result")
            if isinstance(r, str):
                result_text = r
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
    if not re.search(r"<promise>COMPLETE</promise>", text):
        raise PromiseParseError(
            f"Agent produced no <promise>COMPLETE</promise> tag.{tail}"
        )
    return CompletionOutput()
