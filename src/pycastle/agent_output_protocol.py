import dataclasses
import enum
import json
import re
from typing import Literal, TypeAlias, overload


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


def _unwrap(output: str) -> str:
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            result = obj.get("result")
            return result if isinstance(result, str) else output
    return output


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


@overload
def parse(output: str, role: Literal[AgentRole.PLANNER]) -> PlannerOutput: ...


@overload
def parse(output: str, role: Literal[AgentRole.PREFLIGHT_ISSUE]) -> IssueOutput: ...


@overload
def parse(output: str, role: AgentRole) -> AgentOutput: ...


def parse(output: str, role: AgentRole) -> AgentOutput:
    text = _unwrap(output)
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


def assert_complete(output: str) -> None:
    text = _unwrap(output)
    if not re.search(r"<promise>COMPLETE</promise>", text):
        tail = text[-200:]
        raise PromiseParseError(
            f"Agent produced no <promise>COMPLETE</promise> tag. Output tail: {tail!r}"
        )
