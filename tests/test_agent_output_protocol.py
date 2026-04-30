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
    assert_complete,
    parse,
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


# ── parse (single entry point) ────────────────────────────────────────────────


def test_parse_planner_returns_planner_output():
    output = '<plan>{"issues": [{"number": 1, "title": "Fix bug"}]}</plan>'
    result = parse(output, AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)
    assert result.issues == [{"number": 1, "title": "Fix bug"}]


def test_parse_preflight_issue_returns_issue_output():
    output = '<issue>\n{"number": 42, "labels": ["bug", "ready-for-agent"]}\n</issue>'
    result = parse(output, AgentRole.PREFLIGHT_ISSUE)
    assert isinstance(result, IssueOutput)
    assert result.labels == ["bug", "ready-for-agent"]
    assert result.number == 42


def test_parse_preflight_issue_hitl_label():
    output = '<issue>\n{"number": 7, "labels": ["bug", "ready-for-human"]}\n</issue>'
    result = parse(output, AgentRole.PREFLIGHT_ISSUE)
    assert isinstance(result, IssueOutput)
    assert "ready-for-human" in result.labels


def test_parse_implementer_returns_completion_output():
    result = parse("<promise>COMPLETE</promise>", AgentRole.IMPLEMENTER)
    assert isinstance(result, CompletionOutput)


def test_parse_reviewer_returns_completion_output():
    result = parse("<promise>COMPLETE</promise>", AgentRole.REVIEWER)
    assert isinstance(result, CompletionOutput)


def test_parse_merger_returns_completion_output():
    result = parse("<promise>COMPLETE</promise>", AgentRole.MERGER)
    assert isinstance(result, CompletionOutput)


def test_parse_planner_succeeds_without_promise_tag():
    output = '<plan>{"issues": []}</plan>'
    result = parse(output, AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)


def test_parse_preflight_issue_succeeds_without_promise_tag():
    output = '<issue>\n{"number": 1, "labels": ["bug", "ready-for-agent"]}\n</issue>'
    result = parse(output, AgentRole.PREFLIGHT_ISSUE)
    assert isinstance(result, IssueOutput)


def test_parse_raises_promise_parse_error_for_implementer_without_promise():
    with pytest.raises(PromiseParseError):
        parse("work done but no promise tag", AgentRole.IMPLEMENTER)


def test_parse_raises_plan_parse_error_for_planner_without_plan_tag():
    with pytest.raises(PlanParseError):
        parse("no plan here", AgentRole.PLANNER)


def test_parse_raises_issue_parse_error_for_preflight_issue_without_issue_tag():
    with pytest.raises(IssueParseError):
        parse("no issue here", AgentRole.PREFLIGHT_ISSUE)


def test_parse_errors_are_agent_output_protocol_errors():
    with pytest.raises(AgentOutputProtocolError):
        parse("no promise", AgentRole.IMPLEMENTER)


def test_parse_unwraps_ndjson_envelope_for_planner():
    envelope = json.dumps(
        {
            "type": "result",
            "result": '<plan>{"issues": [{"number": 5, "title": "T"}]}</plan>',
        }
    )
    result = parse(envelope, AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)
    assert result.issues == [{"number": 5, "title": "T"}]


def test_parse_unwraps_ndjson_envelope_for_implementer():
    envelope = json.dumps({"type": "result", "result": "<promise>COMPLETE</promise>"})
    result = parse(envelope, AgentRole.IMPLEMENTER)
    assert isinstance(result, CompletionOutput)


def test_parse_falls_back_to_raw_string_when_no_ndjson():
    output = '<plan>{"issues": [{"number": 2, "title": "X"}]}</plan>'
    result = parse(output, AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)


def test_parse_planner_accepts_unblocked_issues_key():
    output = '<plan>{"unblocked_issues": [{"number": 3, "title": "Y"}]}</plan>'
    result = parse(output, AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)
    assert result.issues == [{"number": 3, "title": "Y"}]


def test_parse_empty_string_raises_promise_parse_error():
    with pytest.raises(PromiseParseError):
        parse("", AgentRole.IMPLEMENTER)


def test_parse_planner_with_malformed_json_raises_plan_parse_error():
    with pytest.raises(PlanParseError, match="malformed JSON"):
        parse("<plan>not json</plan>", AgentRole.PLANNER)


def test_parse_planner_with_empty_issues_returns_empty_planner_output():
    output = '<plan>{"issues": []}</plan>'
    result = parse(output, AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)
    assert result.issues == []


def test_parse_preflight_issue_with_non_integer_raises_issue_parse_error():
    output = '<issue>{"number": "abc", "labels": ["bug"]}</issue>'
    with pytest.raises(IssueParseError):
        parse(output, AgentRole.PREFLIGHT_ISSUE)


def test_parse_preflight_issue_with_malformed_json_raises_issue_parse_error():
    with pytest.raises(IssueParseError, match="Malformed JSON"):
        parse("<issue>not json</issue>", AgentRole.PREFLIGHT_ISSUE)


def test_parse_unwraps_ndjson_envelope_for_preflight_issue():
    envelope = json.dumps(
        {
            "type": "result",
            "result": '<issue>{"number": 99, "labels": ["bug", "ready-for-agent"]}</issue>',
        }
    )
    result = parse(envelope, AgentRole.PREFLIGHT_ISSUE)
    assert isinstance(result, IssueOutput)
    assert result.number == 99


def test_parse_raises_promise_parse_error_for_reviewer_without_promise():
    with pytest.raises(PromiseParseError):
        parse("reviewed but forgot promise", AgentRole.REVIEWER)


def test_parse_raises_promise_parse_error_for_merger_without_promise():
    with pytest.raises(PromiseParseError):
        parse("merged but forgot promise", AgentRole.MERGER)


def test_parse_preflight_issue_with_missing_number_key_raises_issue_parse_error():
    output = '<issue>{"labels": ["bug"]}</issue>'
    with pytest.raises(IssueParseError):
        parse(output, AgentRole.PREFLIGHT_ISSUE)


def test_parse_preflight_issue_with_missing_labels_key_raises_issue_parse_error():
    output = '<issue>{"number": 42}</issue>'
    with pytest.raises(IssueParseError):
        parse(output, AgentRole.PREFLIGHT_ISSUE)


def test_parse_planner_with_plan_json_missing_both_keys_raises_plan_parse_error():
    output = '<plan>{"something_else": []}</plan>'
    with pytest.raises(PlanParseError):
        parse(output, AgentRole.PLANNER)


def test_parse_planner_with_non_dict_json_raises_plan_parse_error():
    output = '<plan>["issue1", "issue2"]</plan>'
    with pytest.raises(PlanParseError):
        parse(output, AgentRole.PLANNER)


def test_parse_planner_with_issues_missing_title_raises_plan_parse_error():
    output = '<plan>{"issues": [{"number": 1}]}</plan>'
    with pytest.raises(PlanParseError):
        parse(output, AgentRole.PLANNER)


def test_parse_planner_with_issues_missing_number_raises_plan_parse_error():
    output = '<plan>{"issues": [{"title": "Fix bug"}]}</plan>'
    with pytest.raises(PlanParseError):
        parse(output, AgentRole.PLANNER)


def test_parse_ndjson_with_null_result_falls_back_to_envelope_and_raises():
    envelope = json.dumps({"type": "result", "result": None})
    with pytest.raises(PlanParseError):
        parse(envelope, AgentRole.PLANNER)


# ── assert_complete ───────────────────────────────────────────────────────────


def test_assert_complete_returns_none_on_success():
    result = assert_complete("<promise>COMPLETE</promise>")
    assert result is None


def test_assert_complete_raises_promise_parse_error_on_missing_tag():
    with pytest.raises(PromiseParseError):
        assert_complete("work done but no promise tag")


def test_assert_complete_unwraps_ndjson_envelope():
    envelope = json.dumps({"type": "result", "result": "<promise>COMPLETE</promise>"})
    result = assert_complete(envelope)
    assert result is None


def test_assert_complete_raises_on_ndjson_missing_promise():
    envelope = json.dumps({"type": "result", "result": "work done"})
    with pytest.raises(PromiseParseError):
        assert_complete(envelope)


def test_assert_complete_error_message_includes_output_tail():
    long_output = "x" * 300 + " work done but no tag"
    with pytest.raises(PromiseParseError) as exc_info:
        assert_complete(long_output)
    assert "no tag" in str(exc_info.value)


def test_assert_complete_raises_on_empty_string():
    with pytest.raises(PromiseParseError):
        assert_complete("")


def test_assert_complete_raises_when_ndjson_result_is_null():
    envelope = json.dumps({"type": "result", "result": None})
    with pytest.raises(PromiseParseError):
        assert_complete(envelope)
