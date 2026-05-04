import dataclasses
import json

import pytest

from pycastle.agent_output_protocol import (
    AgentOutput,
    AgentOutputProtocolError,
    AgentRole,
    CompletionOutput,
    IssueOutput,
    IssueParseError,
    PlanParseError,
    PlannerOutput,
    PromiseParseError,
    process_stream,
)
from pycastle.errors import UsageLimitError


def _result_line(content: str) -> str:
    return json.dumps(
        {"type": "result", "subtype": "success", "result": content, "is_error": False}
    )


def _assistant_line(text: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        }
    )


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_plan_parse_error_is_subclass_of_base():
    assert issubclass(PlanParseError, AgentOutputProtocolError)


def test_issue_parse_error_is_subclass_of_base():
    assert issubclass(IssueParseError, AgentOutputProtocolError)


def test_promise_parse_error_is_subclass_of_base():
    assert issubclass(PromiseParseError, AgentOutputProtocolError)


# ── AgentRole ─────────────────────────────────────────────────────────────────


def test_agent_role_has_all_five_members():
    members = {r.name for r in AgentRole}
    assert members == {
        "PLANNER",
        "PREFLIGHT_ISSUE",
        "IMPLEMENTER",
        "REVIEWER",
        "MERGER",
    }


def test_agent_role_values_are_snake_case_strings():
    assert AgentRole.PLANNER.value == "planner"
    assert AgentRole.PREFLIGHT_ISSUE.value == "preflight_issue"
    assert AgentRole.IMPLEMENTER.value == "implementer"
    assert AgentRole.REVIEWER.value == "reviewer"
    assert AgentRole.MERGER.value == "merger"


# ── PlannerOutput ─────────────────────────────────────────────────────────────


def test_planner_output_stores_issues():
    issues = [{"number": 1, "title": "Fix bug"}]
    out = PlannerOutput(issues=issues)
    assert out.issues == issues


def test_planner_output_is_frozen():
    out = PlannerOutput(issues=[])
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.issues = []  # type: ignore[misc]


# ── IssueOutput ───────────────────────────────────────────────────────────────


def test_issue_output_stores_labels_and_number():
    out = IssueOutput(labels=["bug", "ready-for-agent"], number=42)
    assert out.labels == ["bug", "ready-for-agent"]
    assert out.number == 42


def test_issue_output_is_frozen():
    out = IssueOutput(labels=["bug"], number=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.number = 2  # type: ignore[misc]


# ── CompletionOutput ──────────────────────────────────────────────────────────


def test_completion_output_is_instantiable():
    out = CompletionOutput()
    assert isinstance(out, CompletionOutput)


def test_completion_output_is_frozen():
    out = CompletionOutput()
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.foo = "bar"  # type: ignore[attr-defined]


# ── AgentOutput type alias ────────────────────────────────────────────────────


def test_agent_output_covers_all_output_types():
    planner: AgentOutput = PlannerOutput(issues=[])
    issue: AgentOutput = IssueOutput(labels=["bug"], number=1)
    completion: AgentOutput = CompletionOutput()
    assert isinstance(planner, PlannerOutput)
    assert isinstance(issue, IssueOutput)
    assert isinstance(completion, CompletionOutput)


# ── process_stream ────────────────────────────────────────────────────────────


def test_process_stream_planner_returns_planner_output():
    lines = [
        _result_line('<plan>{"issues": [{"number": 1, "title": "Fix bug"}]}</plan>')
    ]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)
    assert result.issues == [{"number": 1, "title": "Fix bug"}]


def test_process_stream_preflight_issue_returns_issue_output():
    lines = [
        _result_line(
            '<issue>{"number": 42, "labels": ["bug", "ready-for-agent"]}</issue>'
        )
    ]
    result = process_stream(
        lines,
        on_turn=lambda t: None,
        role=AgentRole.PREFLIGHT_ISSUE,
    )
    assert isinstance(result, IssueOutput)
    assert result.number == 42
    assert result.labels == ["bug", "ready-for-agent"]


def test_process_stream_implementer_returns_completion_output():
    lines = [_result_line("<promise>COMPLETE</promise>")]
    result = process_stream(
        lines,
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
    )
    assert isinstance(result, CompletionOutput)


def test_process_stream_reviewer_returns_completion_output():
    lines = [_result_line("<promise>COMPLETE</promise>")]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.REVIEWER)
    assert isinstance(result, CompletionOutput)


def test_process_stream_merger_returns_completion_output():
    lines = [_result_line("<promise>COMPLETE</promise>")]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.MERGER)
    assert isinstance(result, CompletionOutput)


def test_process_stream_raises_usage_limit_error_on_429_json():
    error_line = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "api_error_status": 429,
            "result": "rate limited",
        }
    )
    with pytest.raises(UsageLimitError) as exc_info:
        process_stream(
            [error_line],
            on_turn=lambda t: None,
            role=AgentRole.IMPLEMENTER,
        )
    assert exc_info.value.reset_time is None


def test_process_stream_usage_limit_carries_parsed_reset_time():
    error_line = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "api_error_status": 429,
            "result": "You're out of extra usage · resets 12:50pm (UTC)",
        }
    )
    with pytest.raises(UsageLimitError) as exc_info:
        process_stream(
            [error_line],
            on_turn=lambda t: None,
            role=AgentRole.IMPLEMENTER,
        )
    reset = exc_info.value.reset_time
    assert reset is not None
    assert (reset.hour, reset.minute) != (0, 0) or reset.minute == 50


def test_process_stream_invokes_on_turn_for_each_assistant_turn():
    turns: list[str] = []
    process_stream(
        [
            _assistant_line("Hello, I will fix this."),
            _result_line("<promise>COMPLETE</promise>"),
        ],
        on_turn=turns.append,
        role=AgentRole.IMPLEMENTER,
    )
    assert turns == ["Hello, I will fix this."]


def test_process_stream_invokes_on_turn_once_per_turn():
    turns: list[str] = []
    process_stream(
        [
            _assistant_line("First turn."),
            _assistant_line("Second turn."),
            _result_line("<promise>COMPLETE</promise>"),
        ],
        on_turn=turns.append,
        role=AgentRole.IMPLEMENTER,
    )
    assert turns == ["First turn.", "Second turn."]


def test_process_stream_raises_plan_parse_error_when_plan_tag_absent():
    lines = [_result_line("no plan here")]
    with pytest.raises(PlanParseError):
        process_stream(
            lines,
            on_turn=lambda t: None,
            role=AgentRole.PLANNER,
        )


def test_process_stream_raises_issue_parse_error_when_issue_tag_absent():
    lines = [_result_line("no issue tag")]
    with pytest.raises(IssueParseError):
        process_stream(
            lines,
            on_turn=lambda t: None,
            role=AgentRole.PREFLIGHT_ISSUE,
        )


def test_process_stream_raises_promise_parse_error_when_completion_tag_absent():
    lines = [_result_line("work done but no tag")]
    with pytest.raises(PromiseParseError):
        process_stream(
            lines,
            on_turn=lambda t: None,
            role=AgentRole.IMPLEMENTER,
        )


def test_process_stream_extracts_result_from_envelope():
    lines = [
        _assistant_line("thinking"),
        _result_line('<plan>{"issues": [{"number": 7, "title": "T"}]}</plan>'),
    ]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)
    assert result.issues == [{"number": 7, "title": "T"}]


def test_process_stream_raises_usage_limit_immediately_before_end():
    usage_line = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "api_error_status": 429,
            "result": "rate limited",
        }
    )
    result_line = _result_line("<promise>COMPLETE</promise>")
    with pytest.raises(UsageLimitError):
        process_stream(
            [usage_line, result_line],
            on_turn=lambda t: None,
            role=AgentRole.IMPLEMENTER,
        )


def test_process_stream_empty_stream_raises_promise_parse_error():
    with pytest.raises(PromiseParseError):
        process_stream(
            [],
            on_turn=lambda t: None,
            role=AgentRole.IMPLEMENTER,
        )


def test_process_stream_empty_stream_raises_plan_parse_error():
    with pytest.raises(PlanParseError):
        process_stream([], on_turn=lambda t: None, role=AgentRole.PLANNER)


def test_process_stream_empty_stream_raises_issue_parse_error():
    with pytest.raises(IssueParseError):
        process_stream(
            [],
            on_turn=lambda t: None,
            role=AgentRole.PREFLIGHT_ISSUE,
        )


def test_process_stream_no_result_envelope_falls_back_to_collected_lines():
    lines = ["<promise>COMPLETE</promise>"]
    result = process_stream(
        lines,
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
    )
    assert isinstance(result, CompletionOutput)


def test_process_stream_error_message_includes_output_tail():
    long_content = "x" * 300 + " distinctive-tail"
    with pytest.raises(PromiseParseError) as exc_info:
        process_stream(
            [_result_line(long_content)],
            on_turn=lambda t: None,
            role=AgentRole.IMPLEMENTER,
        )
    assert "distinctive-tail" in str(exc_info.value)


def test_process_stream_multiple_text_blocks_assembled_with_double_newline():
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "First block"},
                    {"type": "text", "text": "Second block"},
                ]
            },
        }
    )
    turns: list[str] = []
    process_stream(
        [line, _result_line("<promise>COMPLETE</promise>")],
        on_turn=turns.append,
        role=AgentRole.IMPLEMENTER,
    )
    assert turns == ["First block\n\nSecond block"]


def test_process_stream_does_not_raise_usage_limit_on_plain_text_match():
    with pytest.raises(PromiseParseError):
        process_stream(
            ["CLAUDE REACHED ITS USAGE LIMIT"],
            on_turn=lambda t: None,
            role=AgentRole.IMPLEMENTER,
        )


def test_process_stream_does_not_raise_on_non_429_error_result():
    error_line = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "api_error_status": 529,
            "result": "overloaded: usage limit exceeded",
        }
    )
    with pytest.raises(PromiseParseError):
        process_stream(
            [error_line],
            on_turn=lambda t: None,
            role=AgentRole.IMPLEMENTER,
        )


def test_process_stream_null_result_in_envelope_falls_back_to_collected_lines():
    null_result_line = json.dumps(
        {"type": "result", "subtype": "success", "result": None, "is_error": False}
    )
    lines = [null_result_line, "<promise>COMPLETE</promise>"]
    result = process_stream(
        lines,
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
    )
    assert isinstance(result, CompletionOutput)


def test_process_stream_first_result_envelope_wins_when_multiple_present():
    lines = [
        _result_line('<plan>{"issues": [{"number": 1, "title": "First"}]}</plan>'),
        _result_line('<plan>{"issues": [{"number": 2, "title": "Last"}]}</plan>'),
    ]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)
    assert result.issues == [{"number": 1, "title": "First"}]


def test_process_stream_implementer_stops_consuming_after_turn_with_promise():
    consumed: list[str] = []

    def tracking_iter():
        for line in [
            _assistant_line("<promise>COMPLETE</promise>"),
            _assistant_line("This line must not be consumed"),
            _result_line("<promise>COMPLETE</promise>"),
        ]:
            consumed.append(line)
            yield line

    result = process_stream(
        tracking_iter(),
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
    )
    assert isinstance(result, CompletionOutput)
    assert len(consumed) == 1


def test_process_stream_planner_stops_consuming_after_turn_with_plan():
    consumed: list[str] = []

    def tracking_iter():
        for line in [
            _assistant_line('<plan>{"issues": [{"number": 1, "title": "T"}]}</plan>'),
            _assistant_line("This line must not be consumed"),
            _result_line('<plan>{"issues": [{"number": 2, "title": "T2"}]}</plan>'),
        ]:
            consumed.append(line)
            yield line

    result = process_stream(
        tracking_iter(),
        on_turn=lambda t: None,
        role=AgentRole.PLANNER,
    )
    assert isinstance(result, PlannerOutput)
    assert result.issues == [{"number": 1, "title": "T"}]
    assert len(consumed) == 1


def test_process_stream_preflight_issue_stops_consuming_after_turn_with_issue():
    consumed: list[str] = []

    def tracking_iter():
        for line in [
            _assistant_line('<issue>{"number": 7, "labels": ["bug"]}</issue>'),
            _assistant_line("This line must not be consumed"),
            _result_line('<issue>{"number": 8, "labels": ["other"]}</issue>'),
        ]:
            consumed.append(line)
            yield line

    result = process_stream(
        tracking_iter(),
        on_turn=lambda t: None,
        role=AgentRole.PREFLIGHT_ISSUE,
    )
    assert isinstance(result, IssueOutput)
    assert result.number == 7
    assert len(consumed) == 1


def test_process_stream_stops_consuming_after_result_line():
    consumed: list[str] = []

    def tracking_iter():
        for line in [
            _result_line("<promise>COMPLETE</promise>"),
            _assistant_line("This line must not be consumed"),
        ]:
            consumed.append(line)
            yield line

    result = process_stream(
        tracking_iter(),
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
    )
    assert isinstance(result, CompletionOutput)
    assert len(consumed) == 1


def test_process_stream_planner_skips_malformed_turn_and_exits_on_later_valid_turn():
    lines = [
        _assistant_line("no plan tag here"),
        _assistant_line('<plan>{"issues": [{"number": 3, "title": "Real"}]}</plan>'),
        _assistant_line("This line must not be consumed"),
    ]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)
    assert result.issues == [{"number": 3, "title": "Real"}]


def test_process_stream_preflight_issue_skips_malformed_turn_and_exits_on_later_valid_turn():
    lines = [
        _assistant_line("no issue tag here"),
        _assistant_line('<issue>{"number": 5, "labels": ["bug"]}</issue>'),
        _assistant_line("This line must not be consumed"),
    ]
    result = process_stream(
        lines,
        on_turn=lambda t: None,
        role=AgentRole.PREFLIGHT_ISSUE,
    )
    assert isinstance(result, IssueOutput)
    assert result.number == 5


def test_process_stream_on_turn_receives_signal_turn_before_early_exit():
    received: list[str] = []
    process_stream(
        [_assistant_line("<promise>COMPLETE</promise>")],
        on_turn=received.append,
        role=AgentRole.IMPLEMENTER,
    )
    assert received == ["<promise>COMPLETE</promise>"]


def test_process_stream_non_error_result_with_pattern_text_does_not_raise():
    success_result = json.dumps(
        {
            "type": "result",
            "is_error": False,
            "result": "<promise>COMPLETE</promise> usage limit in text",
        }
    )
    result = process_stream(
        [success_result],
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
    )
    assert isinstance(result, CompletionOutput)
