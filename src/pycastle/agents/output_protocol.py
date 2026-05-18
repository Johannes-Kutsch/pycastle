import dataclasses
import enum
import json
import re
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol, TypeAlias

if TYPE_CHECKING:
    from ..services.agent_service import ParsedTurn

from ..errors import UsageLimitError


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class AgentRole(enum.Enum):
    PLANNER = "planner"
    PREFLIGHT_ISSUE = "preflight_issue"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    MERGER = "merger"
    IMPROVE = "improve"
    FAILURE_REPORT = "failure_report"


@dataclasses.dataclass(frozen=True)
class PlannerOutput:
    issues: list[dict]
    blocked: list[dict] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class IssueOutput:
    labels: list[str]
    number: int


@dataclasses.dataclass(frozen=True)
class CompletionOutput:
    # Bare-integer <issue>N</issue> tags captured from the COMPLETE turn.
    # Phase 03 (improve sub-issues) emits one per filed sub-issue; improve_phase
    # ignores them. Phase 02 emits a JSON-form <issue> tag instead and surfaces
    # as IssueOutput.
    issue_numbers: tuple[int, ...] = ()


@dataclasses.dataclass(frozen=True)
class NoCandidateOutput:
    pass


@dataclasses.dataclass(frozen=True)
class CommitMessageOutput:
    message: str | None


@dataclasses.dataclass(frozen=True)
class FailedOutput:
    failure_class: str = ""


AgentOutput: TypeAlias = (
    PlannerOutput
    | IssueOutput
    | CompletionOutput
    | NoCandidateOutput
    | CommitMessageOutput
    | FailedOutput
)

AgentSuccessOutput: TypeAlias = (
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


def _strip_markdown_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[^\n]*\n?", "", s)
        s = re.sub(r"\n?```\s*$", "", s)
    return s.strip()


def _iter_tag_block_candidates(text: str, tag: str) -> Iterable[str]:
    # Yield candidate bodies for <tag>...</tag>, anchored on the LAST </tag>
    # in the text and trying each preceding <tag> opening from the rightmost
    # outward. This lets the parser recover when:
    #   - agent commentary contains a stray <tag> mention before the real
    #     block (a regex `<tag>(.*?)</tag>` would anchor on that first
    #     mention and capture prose instead of the real payload), and
    #   - the real payload itself contains a literal <tag> substring (e.g.
    #     an issue title quoted inside JSON), so the rightmost opening is
    #     not necessarily the real one — callers retry until parsing
    #     succeeds.
    open_tag = f"<{tag}>"
    close_tag = f"</{tag}>"
    end = text.rfind(close_tag)
    if end == -1:
        return
    pos = end
    while True:
        start = text.rfind(open_tag, 0, pos)
        if start == -1:
            return
        yield text[start + len(open_tag) : end]
        pos = start


def _last_tag_block(text: str, tag: str) -> str | None:
    for body in _iter_tag_block_candidates(text, tag):
        return body
    return None


def _extract_planner_output(text: str) -> PlannerOutput:
    last_err: PlanParseError | None = None
    saw_block = False
    for body in _iter_tag_block_candidates(text, "plan"):
        saw_block = True
        try:
            return _parse_planner_body(body)
        except PlanParseError as exc:
            last_err = exc
    if not saw_block:
        raise PlanParseError("Planner produced no <plan> tag.")
    assert last_err is not None
    raise last_err


def _parse_planner_body(body: str) -> PlannerOutput:
    try:
        data = json.loads(_strip_markdown_fence(body))
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
        issues = [{"number": i["number"], "title": i["title"]} for i in raw]
    except (KeyError, TypeError) as exc:
        raise PlanParseError(
            f"Plan JSON issues list has unexpected structure: {exc}"
        ) from exc
    raw_blocked = data.get("blocked", [])
    try:
        blocked = [
            {
                "number": b["number"],
                "blocked_by": b["blocked_by"],
                "reason": b["reason"],
            }
            for b in raw_blocked
        ]
    except (KeyError, TypeError) as exc:
        raise PlanParseError(
            f"Plan JSON blocked list has unexpected structure: {exc}"
        ) from exc
    return PlannerOutput(issues=issues, blocked=blocked)


def _extract_issue_output(text: str) -> IssueOutput:
    last_err: IssueParseError | None = None
    saw_block = False
    for body in _iter_tag_block_candidates(text, "issue"):
        saw_block = True
        try:
            return _parse_issue_body(body)
        except IssueParseError as exc:
            last_err = exc
    if not saw_block:
        raise IssueParseError("Agent produced no <issue>...</issue> tag.")
    assert last_err is not None
    raise last_err


def _parse_issue_body(body: str) -> IssueOutput:
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise IssueParseError(f"Malformed JSON inside <issue> tag: {exc}") from exc
    try:
        number = int(data["number"])
        labels = [str(label) for label in data["labels"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise IssueParseError(f"<issue> JSON has unexpected structure: {exc}") from exc
    return IssueOutput(labels=labels, number=number)


_ISSUE_NUMBER_RE = re.compile(r"<issue>(\d+)</issue>")


def _extract_issue_numbers(text: str) -> tuple[int, ...]:
    return tuple(int(m.group(1)) for m in _ISSUE_NUMBER_RE.finditer(text))


def _extract_improve_output(text: str) -> IssueOutput | CompletionOutput:
    # Phase 02 (PRD) emits a JSON-form <issue>; phase 03 emits bare integers.
    try:
        return _extract_issue_output(text)
    except IssueParseError:
        return CompletionOutput(issue_numbers=_extract_issue_numbers(text))


class _RoleHandler(Protocol):
    def check_turn(self, turn: str) -> AgentOutput | None: ...
    def extract_final(self, text: str, tail: str) -> AgentOutput: ...


class _CommitMessageHandler:
    def check_turn(self, turn: str) -> AgentOutput | None:
        body = _last_tag_block(turn, "commit_message")
        if body is not None:
            return CommitMessageOutput(message=body.strip())
        return None

    def extract_final(self, text: str, tail: str) -> AgentOutput:
        body = _last_tag_block(text, "commit_message")
        if body is None:
            return CommitMessageOutput(message=None)
        return CommitMessageOutput(message=body.strip())


class _PlannerHandler:
    def check_turn(self, turn: str) -> AgentOutput | None:
        try:
            return _extract_planner_output(turn)
        except PlanParseError:
            return None

    def extract_final(self, text: str, tail: str) -> AgentOutput:
        try:
            return _extract_planner_output(text)
        except PlanParseError as exc:
            raise PlanParseError(f"{exc}{tail}") from exc.__cause__


class _PreflightIssueHandler:
    def check_turn(self, turn: str) -> AgentOutput | None:
        try:
            return _extract_issue_output(turn)
        except IssueParseError:
            return None

    def extract_final(self, text: str, tail: str) -> AgentOutput:
        try:
            return _extract_issue_output(text)
        except IssueParseError as exc:
            raise IssueParseError(f"{exc}{tail}") from exc.__cause__


class _MergerHandler:
    def check_turn(self, turn: str) -> AgentOutput | None:
        if re.search(r"<promise>FAILED</promise>", turn):
            return FailedOutput()
        if re.search(r"<promise>COMPLETE</promise>", turn):
            return CompletionOutput()
        return None

    def extract_final(self, text: str, tail: str) -> AgentOutput:
        if re.search(r"<promise>FAILED</promise>", text):
            return FailedOutput()
        if not re.search(r"<promise>COMPLETE</promise>", text):
            raise PromiseParseError(
                f"Agent produced no <promise>COMPLETE</promise> tag.{tail}"
            )
        return CompletionOutput()


class _ImproveHandler:
    def check_turn(self, turn: str) -> AgentOutput | None:
        if re.search(r"<promise>FAILED</promise>", turn):
            return FailedOutput()
        if re.search(r"<promise>NO-CANDIDATE</promise>", turn):
            return NoCandidateOutput()
        if re.search(r"<promise>COMPLETE</promise>", turn):
            return _extract_improve_output(turn)
        return None

    def extract_final(self, text: str, tail: str) -> AgentOutput:
        if re.search(r"<promise>FAILED</promise>", text):
            return FailedOutput()
        if re.search(r"<promise>NO-CANDIDATE</promise>", text):
            return NoCandidateOutput()
        if not re.search(r"<promise>COMPLETE</promise>", text):
            raise PromiseParseError(
                f"Agent produced no <promise>COMPLETE</promise> or"
                f" <promise>NO-CANDIDATE</promise> tag.{tail}"
            )
        return _extract_improve_output(text)


_commit_message_handler = _CommitMessageHandler()
_preflight_issue_handler = _PreflightIssueHandler()

_HANDLERS: dict[AgentRole, _RoleHandler] = {
    AgentRole.IMPLEMENTER: _commit_message_handler,
    AgentRole.REVIEWER: _commit_message_handler,
    AgentRole.PLANNER: _PlannerHandler(),
    AgentRole.PREFLIGHT_ISSUE: _preflight_issue_handler,
    AgentRole.FAILURE_REPORT: _preflight_issue_handler,
    AgentRole.IMPROVE: _ImproveHandler(),
    AgentRole.MERGER: _MergerHandler(),
}

assert len(_HANDLERS) == len(AgentRole)


def process_stream_from_events(
    events: "Iterable[ParsedTurn]",
    on_turn: Callable[[str], None],
    role: AgentRole,
    on_tokens: Callable[[int], None] | None = None,
) -> AgentOutput:
    from ..services.agent_service import AssistantTurn, Result, Tokens, UsageLimit

    handler = _HANDLERS[role]
    result_text: str | None = None
    collected_turns: list[str] = []
    for event in events:
        if isinstance(event, UsageLimit):
            raise UsageLimitError(reset_time=event.reset_time)
        elif isinstance(event, Tokens):
            if on_tokens is not None:
                on_tokens(event.count)
        elif isinstance(event, AssistantTurn):
            on_turn(event.text)
            collected_turns.append(event.text)
            result = handler.check_turn(event.text)
            if result is not None:
                return result
        elif isinstance(event, Result):
            result_text = event.text
            break
    text = result_text if result_text is not None else "\n".join(collected_turns)
    tail = f"\nOutput tail: {text[-300:]!r}"
    return handler.extract_final(text, tail)


def process_stream(
    lines: Iterable[str],
    on_turn: Callable[[str], None],
    role: AgentRole,
    on_tokens: Callable[[int], None] | None = None,
) -> AgentOutput:
    from ..services.claude_service import ClaudeService

    return process_stream_from_events(
        ClaudeService().run(lines), on_turn, role, on_tokens
    )
