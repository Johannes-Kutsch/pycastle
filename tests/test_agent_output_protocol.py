import json

import pytest

from pycastle.agent_output_protocol import (
    AgentOutputProtocolError,
    IssueParseError,
    PlanParseError,
    PromiseParseError,
    is_complete,
    parse_issue_number,
    parse_plan,
)


# ── _unwrap (tested indirectly through public functions) ──────────────────────


def test_parse_plan_unwraps_ndjson_result():
    envelope = json.dumps(
        {
            "type": "result",
            "result": '<plan>{"issues": [{"number": 1, "title": "Fix"}]}</plan>',
        }
    )
    assert parse_plan(envelope) == [{"number": 1, "title": "Fix"}]


def test_parse_plan_falls_back_to_raw_string_when_no_ndjson():
    output = '<plan>{"issues": [{"number": 2, "title": "Bug"}]}</plan>'
    assert parse_plan(output) == [{"number": 2, "title": "Bug"}]


# ── parse_plan ────────────────────────────────────────────────────────────────


def test_parse_plan_returns_issues_list():
    output = '<plan>{"issues": [{"number": 1, "title": "Fix bug"}]}</plan>'
    assert parse_plan(output) == [{"number": 1, "title": "Fix bug"}]


def test_parse_plan_accepts_unblocked_issues_key():
    output = (
        '<plan>{"unblocked_issues": [{"number": 3, "title": "Add feature"}]}</plan>'
    )
    assert parse_plan(output) == [{"number": 3, "title": "Add feature"}]


def test_parse_plan_returns_empty_list_when_no_issues():
    output = '<plan>{"issues": []}</plan>'
    assert parse_plan(output) == []


def test_parse_plan_raises_on_missing_plan_tag():
    with pytest.raises(PlanParseError, match="<plan>"):
        parse_plan("some output with no plan tag")


def test_parse_plan_raises_on_malformed_json():
    with pytest.raises(PlanParseError, match="malformed JSON"):
        parse_plan("<plan>not valid json</plan>")


def test_parse_plan_raises_when_neither_key_present():
    with pytest.raises(PlanParseError, match="unblocked_issues"):
        parse_plan('<plan>{"other_key": []}</plan>')


def test_parse_plan_raises_when_json_is_not_a_dict():
    with pytest.raises(PlanParseError):
        parse_plan("<plan>[]</plan>")


def test_parse_plan_raises_when_issues_is_not_iterable():
    with pytest.raises(PlanParseError):
        parse_plan('<plan>{"issues": 42}</plan>')


def test_parse_plan_raises_when_issue_items_missing_required_keys():
    with pytest.raises(PlanParseError):
        parse_plan('<plan>{"issues": [{}]}</plan>')


def test_parse_plan_error_is_agent_output_protocol_error():
    with pytest.raises(AgentOutputProtocolError):
        parse_plan("no plan tag here")


# ── parse_issue_number ────────────────────────────────────────────────────────


def test_parse_issue_number_returns_verdict_and_number_for_afk():
    output = '<issue label="ready-for-agent">42</issue>'
    verdict, number = parse_issue_number(output)
    assert verdict == "ready-for-agent"
    assert number == 42


def test_parse_issue_number_returns_verdict_and_number_for_hitl():
    output = '<issue label="ready-for-human">99</issue>'
    verdict, number = parse_issue_number(output)
    assert verdict == "ready-for-human"
    assert number == 99


def test_parse_issue_number_raises_on_missing_tag():
    with pytest.raises(IssueParseError, match="issue"):
        parse_issue_number("no issue tag here")


def test_parse_issue_number_raises_when_content_not_integer():
    with pytest.raises(IssueParseError, match="not a valid issue number"):
        parse_issue_number('<issue label="ready-for-agent">abc</issue>')


def test_parse_issue_number_unwraps_ndjson():
    envelope = json.dumps(
        {"type": "result", "result": '<issue label="ready-for-human">7</issue>'}
    )
    verdict, number = parse_issue_number(envelope)
    assert verdict == "ready-for-human"
    assert number == 7


def test_parse_issue_number_error_is_agent_output_protocol_error():
    with pytest.raises(AgentOutputProtocolError):
        parse_issue_number("no tag")


# ── is_complete ───────────────────────────────────────────────────────────────


def test_is_complete_returns_true_when_promise_present():
    assert is_complete("some text <promise>COMPLETE</promise> more text") is True


def test_is_complete_returns_false_when_absent():
    assert is_complete("no promise here") is False


def test_is_complete_returns_false_for_partial_promise():
    assert is_complete("<promise>INCOMPLETE</promise>") is False


def test_is_complete_never_raises_on_empty_string():
    assert is_complete("") is False


def test_is_complete_never_raises_on_arbitrary_input():
    assert is_complete("<promise></promise>") is False


def test_is_complete_falls_back_to_raw_when_result_field_is_null():
    envelope = json.dumps({"type": "result", "result": None})
    assert is_complete(envelope) is False


def test_is_complete_unwraps_ndjson():
    envelope = json.dumps({"type": "result", "result": "<promise>COMPLETE</promise>"})
    assert is_complete(envelope) is True


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_plan_parse_error_is_subclass_of_base():
    assert issubclass(PlanParseError, AgentOutputProtocolError)


def test_issue_parse_error_is_subclass_of_base():
    assert issubclass(IssueParseError, AgentOutputProtocolError)


def test_promise_parse_error_is_subclass_of_base():
    assert issubclass(PromiseParseError, AgentOutputProtocolError)
