import dataclasses
import json
from datetime import datetime, timezone

import pytest

import pycastle._time as _time_module
from pycastle.agents.output_protocol import (
    AgentOutput,
    AgentOutputProtocolError,
    AgentRole,
    BehaviorOutput,
    CommitMessageOutput,
    CompletionOutput,
    FailedOutput,
    IssueOutput,
    IssueParseError,
    NoCandidateOutput,
    PlanParseError,
    PlannerOutput,
    PromiseParseError,
    process_stream,
    process_stream_from_events,
)
from pycastle.errors import TransientAgentError, UsageLimitError
from pycastle.services.agent_service import (
    AssistantTurn,
    Result,
)


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


def _assistant_line_with_usage(
    text: str,
    input_tokens: int,
    cache_creation: int = 0,
    cache_read: int = 0,
) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": text}],
                "usage": {
                    "input_tokens": input_tokens,
                    "cache_creation_input_tokens": cache_creation,
                    "cache_read_input_tokens": cache_read,
                },
            },
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


def test_agent_role_has_all_members():
    members = {r.name for r in AgentRole}
    assert members == {
        "PLANNER",
        "PREFLIGHT_ISSUE",
        "IMPLEMENTER",
        "REVIEWER",
        "MERGER",
        "IMPROVE",
        "FAILURE_REPORT",
        "DIVERGENCE_RESOLVER",
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


def test_planner_output_stores_blocked():
    blocked = [{"number": 5, "blocked_by": 3, "reason": "depends on fix"}]
    out = PlannerOutput(issues=[], blocked=blocked)
    assert out.blocked == blocked


def test_planner_output_blocked_defaults_to_empty_list():
    out = PlannerOutput(issues=[])
    assert out.blocked == []


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


def test_completion_output_defaults_to_empty_issue_numbers():
    out = CompletionOutput()
    assert out.issue_numbers == ()


def test_completion_output_stores_issue_numbers():
    out = CompletionOutput(issue_numbers=(42, 43))
    assert out.issue_numbers == (42, 43)


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


def test_process_stream_planner_extracts_blocked_when_present():
    payload = json.dumps(
        {
            "issues": [{"number": 2, "title": "B"}],
            "blocked": [{"number": 5, "blocked_by": 3, "reason": "needs fix first"}],
        }
    )
    lines = [_result_line(f"<plan>{payload}</plan>")]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)
    assert result.blocked == [{"number": 5}]


def test_process_stream_planner_accepts_legacy_blocked_entries_with_extra_fields():
    payload = json.dumps(
        {
            "issues": [],
            "blocked": [
                {
                    "number": 5,
                    "title": "Unblock planner parsing",
                    "blocked_by": 3,
                    "reason": "needs fix first",
                }
            ],
        }
    )
    lines = [_result_line(f"<plan>{payload}</plan>")]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)
    assert result.blocked == [{"number": 5, "title": "Unblock planner parsing"}]


def test_process_stream_planner_accepts_concise_blocked_entries():
    payload = json.dumps(
        {
            "issues": [{"number": 2, "title": "B"}],
            "blocked": [{"number": 5, "title": "Unblock planner parsing"}],
        }
    )
    lines = [_result_line(f"<plan>{payload}</plan>")]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)
    assert result.issues == [{"number": 2, "title": "B"}]
    assert result.blocked == [{"number": 5, "title": "Unblock planner parsing"}]


def test_process_stream_planner_accepts_custom_blocked_entries():
    payload = json.dumps(
        {
            "issues": [],
            "blocked": [{"number": 5, "note": "waiting on maintainer"}],
        }
    )
    lines = [_result_line(f"<plan>{payload}</plan>")]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)
    assert result.blocked == [{"number": 5}]


def test_process_stream_planner_defaults_blocked_to_empty_when_absent():
    lines = [_result_line('<plan>{"issues": [{"number": 1, "title": "A"}]}</plan>')]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)
    assert result.blocked == []


def test_process_stream_planner_accepts_number_only_blocked_entries():
    payload = json.dumps(
        {
            "issues": [],
            "blocked": [{"number": 5}],
        }
    )
    lines = [_result_line(f"<plan>{payload}</plan>")]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)
    assert result.blocked == [{"number": 5}]


def test_process_stream_planner_handles_json_wrapped_in_markdown_code_fence():
    payload = json.dumps({"issues": [{"number": 1, "title": "Fix bug"}]})
    fenced = f" ```json\n{payload}\n```"
    lines = [_result_line(f"<plan>{fenced}</plan>")]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)
    assert result.issues == [{"number": 1, "title": "Fix bug"}]


def test_process_stream_planner_handles_json_with_leading_and_trailing_whitespace():
    payload = json.dumps({"issues": [{"number": 2, "title": "A"}]})
    lines = [_result_line(f"<plan>\n  {payload}\n</plan>")]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)
    assert result.issues == [{"number": 2, "title": "A"}]


def test_process_stream_planner_rejects_escaped_json_string_inside_plan_tag():
    payload = json.dumps(
        '{"issues": [{"number": 2, "title": "Escaped string"}], "blocked": []}'
    )
    lines = [_result_line(f"<plan>{payload}</plan>")]
    with pytest.raises(PlanParseError, match=r"Plan JSON must be an object, got str\."):
        process_stream(lines, on_turn=lambda t: None, role=AgentRole.PLANNER)


def test_process_stream_planner_picks_last_plan_block_when_stray_tag_in_prose():
    payload = json.dumps(
        {
            "issues": [
                {
                    "number": 579,
                    "title": "Add blocked field to PlannerOutput and <plan> parser",
                }
            ]
        }
    )
    text = (
        "I will plan issue 579 (titled 'Add blocked field to PlannerOutput "
        "and <plan> parser').\n\n"
        f"<plan>{payload}</plan>"
    )
    lines = [_result_line(text)]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)
    assert result.issues == [
        {
            "number": 579,
            "title": "Add blocked field to PlannerOutput and <plan> parser",
        }
    ]


def test_process_stream_planner_recovers_when_plan_substring_appears_inside_json_title():
    # Mirrors the issue #584 incident: the agent quotes an issue title that
    # contains the literal substring `<plan>` in commentary AND the same
    # title is echoed inside the real plan JSON.
    payload = json.dumps(
        {
            "issues": [
                {
                    "number": 579,
                    "title": "Add blocked field to PlannerOutput and <plan> parser",
                },
                {"number": 580, "title": "Other"},
            ]
        }
    )
    text = (
        "...so they can be worked on in parallel without merge conflict risk.\n\n"
        f"<plan>\n{payload}\n</plan>"
    )
    lines = [_result_line(text)]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)
    assert [i["number"] for i in result.issues] == [579, 580]


def test_process_stream_preflight_issue_picks_last_issue_block_when_stray_tag_in_prose():
    payload = json.dumps({"number": 42, "labels": ["bug"]})
    text = f"I'll triage <issue> below.\n<issue>{payload}</issue>"
    lines = [_result_line(text)]
    result = process_stream(
        lines, on_turn=lambda t: None, role=AgentRole.PREFLIGHT_ISSUE
    )
    assert isinstance(result, IssueOutput)
    assert result.number == 42
    assert result.labels == ["bug"]


def test_process_stream_implementer_picks_last_commit_message_when_stray_tag_in_prose():
    text = (
        "Draft commit_message: I considered <commit_message> earlier.\n"
        "<commit_message>final: fix bug</commit_message>"
    )
    lines = [_result_line(text)]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.IMPLEMENTER)
    assert isinstance(result, CommitMessageOutput)
    assert result.message == "final: fix bug"


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


def test_process_stream_implementer_returns_commit_message_output():
    lines = [_result_line("<commit_message>did the thing</commit_message>")]
    result = process_stream(
        lines,
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
    )
    assert isinstance(result, CommitMessageOutput)
    assert result.message == "did the thing"


def test_process_stream_reviewer_returns_commit_message_output():
    result = process_stream(
        [_result_line("<commit_message>cleaned up</commit_message>")],
        on_turn=lambda t: None,
        role=AgentRole.REVIEWER,
    )
    assert isinstance(result, CommitMessageOutput)
    assert result.message == "cleaned up"


def test_process_stream_reviewer_without_commit_message_returns_none():
    result = process_stream(
        [_result_line("no tags here")],
        on_turn=lambda t: None,
        role=AgentRole.REVIEWER,
    )
    assert isinstance(result, CommitMessageOutput)
    assert result.message is None


def test_process_stream_implementer_returns_none_message_when_tag_absent():
    lines = [_result_line("no commit_message tag here")]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.IMPLEMENTER)
    assert isinstance(result, CommitMessageOutput)
    assert result.message is None


def test_process_stream_reviewer_no_tags_returns_none_message():
    lines = [_result_line("no artifact tags here")]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.REVIEWER)
    assert isinstance(result, CommitMessageOutput)
    assert result.message is None


def test_commit_message_output_is_frozen():
    out = CommitMessageOutput(message="m")
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.message = "x"  # type: ignore[misc]


def test_commit_message_output_accepts_none_message():
    out = CommitMessageOutput(message=None)
    assert out.message is None


def test_process_stream_merger_without_commit_message_returns_none():
    lines = [_result_line("<promise>COMPLETE</promise>")]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.MERGER)
    assert isinstance(result, CommitMessageOutput)
    assert result.message is None


def test_process_stream_merger_returns_commit_message_output():
    lines = [_result_line("<commit_message>resolve active conflict</commit_message>")]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.MERGER)
    assert isinstance(result, CommitMessageOutput)
    assert result.message == "resolve active conflict"


def test_process_stream_divergence_resolver_returns_completion_output():
    lines = [_result_line("<promise>COMPLETE</promise>")]
    result = process_stream(
        lines, on_turn=lambda t: None, role=AgentRole.DIVERGENCE_RESOLVER
    )
    assert isinstance(result, CompletionOutput)


def test_process_stream_divergence_resolver_returns_failed_output():
    lines = [_result_line("<promise>FAILED</promise>")]
    result = process_stream(
        lines, on_turn=lambda t: None, role=AgentRole.DIVERGENCE_RESOLVER
    )
    assert isinstance(result, FailedOutput)


def test_process_stream_improve_complete_returns_completion_output():
    lines = [_result_line("<promise>COMPLETE</promise>")]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.IMPROVE)
    assert isinstance(result, CompletionOutput)
    assert result.issue_numbers == ()


def test_process_stream_improve_captures_issue_numbers_from_complete_turn():
    lines = [
        _result_line("<issue>42</issue><issue>43</issue><promise>COMPLETE</promise>")
    ]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.IMPROVE)
    assert isinstance(result, CompletionOutput)
    assert result.issue_numbers == (42, 43)


def test_process_stream_improve_no_candidate_returns_no_candidate_output():
    lines = [_result_line("<promise>NO-CANDIDATE</promise>")]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.IMPROVE)
    assert isinstance(result, NoCandidateOutput)


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


def _usage_limit_line(text: str) -> str:
    return json.dumps(
        {
            "type": "result",
            "is_error": True,
            "api_error_status": 429,
            "result": text,
        }
    )


def _raise_usage_limit(line: str) -> UsageLimitError:
    with pytest.raises(UsageLimitError) as exc_info:
        process_stream([line], on_turn=lambda t: None, role=AgentRole.IMPLEMENTER)
    return exc_info.value


def _freeze_now(monkeypatch: pytest.MonkeyPatch, now_utc: datetime) -> None:
    frozen = now_utc.astimezone()
    monkeypatch.setattr(_time_module, "now_local", lambda: frozen)


def test_usage_limit_with_date_and_minutes(monkeypatch):
    _freeze_now(monkeypatch, datetime(2026, 5, 4, 8, 0, tzinfo=timezone.utc))
    err = _raise_usage_limit(
        _usage_limit_line("You're out of extra usage · resets May 7, 11:30am (UTC)")
    )
    expected = datetime(2026, 5, 7, 11, 30, tzinfo=timezone.utc).astimezone()
    assert err.reset_time == expected


def test_usage_limit_with_date_hour_only(monkeypatch):
    _freeze_now(monkeypatch, datetime(2026, 5, 4, 8, 0, tzinfo=timezone.utc))
    err = _raise_usage_limit(
        _usage_limit_line("You're out of extra usage · resets May 7, 11am (UTC)")
    )
    expected = datetime(2026, 5, 7, 11, 0, tzinfo=timezone.utc).astimezone()
    assert err.reset_time == expected


def test_usage_limit_no_date_hour_only(monkeypatch):
    _freeze_now(monkeypatch, datetime(2026, 5, 4, 8, 0, tzinfo=timezone.utc))
    err = _raise_usage_limit(_usage_limit_line("resets 11am (UTC)"))
    expected = datetime(2026, 5, 4, 11, 0, tzinfo=timezone.utc).astimezone()
    assert err.reset_time == expected


def test_usage_limit_year_rollover_when_date_more_than_month_in_past(monkeypatch):
    _freeze_now(monkeypatch, datetime(2026, 5, 4, 8, 0, tzinfo=timezone.utc))
    err = _raise_usage_limit(
        _usage_limit_line("You're out of extra usage · resets January 1, 11am (UTC)")
    )
    expected = datetime(2027, 1, 1, 11, 0, tzinfo=timezone.utc).astimezone()
    assert err.reset_time == expected


@pytest.mark.parametrize("month_str", ["Sept", "September", "sep"])
def test_usage_limit_accepts_month_name_variants(monkeypatch, month_str):
    _freeze_now(monkeypatch, datetime(2026, 5, 4, 8, 0, tzinfo=timezone.utc))
    err = _raise_usage_limit(_usage_limit_line(f"resets {month_str} 7, 11am (UTC)"))
    expected = datetime(2026, 9, 7, 11, 0, tzinfo=timezone.utc).astimezone()
    assert err.reset_time == expected


def test_usage_limit_invalid_month_returns_none(monkeypatch):
    _freeze_now(monkeypatch, datetime(2026, 5, 4, 8, 0, tzinfo=timezone.utc))
    err = _raise_usage_limit(_usage_limit_line("resets Smarch 7, 11am (UTC)"))
    assert err.reset_time is None


def test_usage_limit_invalid_day_returns_none(monkeypatch):
    _freeze_now(monkeypatch, datetime(2026, 5, 4, 8, 0, tzinfo=timezone.utc))
    err = _raise_usage_limit(_usage_limit_line("resets Feb 30, 11am (UTC)"))
    assert err.reset_time is None


def test_usage_limit_no_date_grace_window_keeps_today(monkeypatch):
    _freeze_now(monkeypatch, datetime(2026, 5, 4, 11, 1, tzinfo=timezone.utc))
    err = _raise_usage_limit(_usage_limit_line("resets 11:00am (UTC)"))
    expected = datetime(2026, 5, 4, 11, 0, tzinfo=timezone.utc).astimezone()
    assert err.reset_time == expected


def test_process_stream_invokes_on_turn_for_each_assistant_turn():
    turns: list[str] = []
    process_stream(
        [
            _assistant_line("Hello, I will fix this."),
            _result_line("<commit_message>fix</commit_message>"),
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
            _result_line("<commit_message>done</commit_message>"),
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


def test_process_stream_divergence_resolver_raises_when_completion_tag_absent():
    lines = [_result_line("work done but no tag")]
    with pytest.raises(PromiseParseError):
        process_stream(
            lines,
            on_turn=lambda t: None,
            role=AgentRole.DIVERGENCE_RESOLVER,
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
    result_line = _result_line("<commit_message>done</commit_message>")
    with pytest.raises(UsageLimitError):
        process_stream(
            [usage_line, result_line],
            on_turn=lambda t: None,
            role=AgentRole.IMPLEMENTER,
        )


def test_process_stream_empty_stream_returns_none_message_for_merger():
    result = process_stream([], on_turn=lambda t: None, role=AgentRole.MERGER)
    assert isinstance(result, CommitMessageOutput)
    assert result.message is None


def test_process_stream_empty_stream_raises_promise_parse_error_for_divergence_resolver():
    with pytest.raises(PromiseParseError):
        process_stream(
            [],
            on_turn=lambda t: None,
            role=AgentRole.DIVERGENCE_RESOLVER,
        )


def test_process_stream_empty_stream_returns_none_message_for_implementer():
    result = process_stream([], on_turn=lambda t: None, role=AgentRole.IMPLEMENTER)
    assert isinstance(result, CommitMessageOutput)
    assert result.message is None


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
    lines = ["<commit_message>done</commit_message>"]
    result = process_stream(
        lines,
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
    )
    assert isinstance(result, CommitMessageOutput)


def test_process_stream_no_tag_returns_none_message_regardless_of_content():
    long_content = "x" * 300 + " distinctive-tail"
    result = process_stream(
        [_result_line(long_content)],
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
    )
    assert isinstance(result, CommitMessageOutput)
    assert result.message is None


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
        [line, _result_line("<commit_message>done</commit_message>")],
        on_turn=turns.append,
        role=AgentRole.IMPLEMENTER,
    )
    assert turns == ["First block\n\nSecond block"]


def test_process_stream_does_not_raise_usage_limit_on_plain_text_match():
    result = process_stream(
        ["CLAUDE REACHED ITS USAGE LIMIT"],
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
    )
    assert isinstance(result, CommitMessageOutput)
    assert result.message is None


def test_process_stream_raises_transient_agent_error_on_5xx_result():

    error_line = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "api_error_status": 529,
            "result": "overloaded: usage limit exceeded",
        }
    )
    with pytest.raises(TransientAgentError):
        process_stream(
            [error_line],
            on_turn=lambda t: None,
            role=AgentRole.IMPLEMENTER,
        )


def test_process_stream_null_result_in_envelope_falls_back_to_collected_lines():
    null_result_line = json.dumps(
        {"type": "result", "subtype": "success", "result": None, "is_error": False}
    )
    lines = [null_result_line, "<commit_message>done</commit_message>"]
    result = process_stream(
        lines,
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
    )
    assert isinstance(result, CommitMessageOutput)


def test_process_stream_first_result_envelope_wins_when_multiple_present():
    lines = [
        _result_line('<plan>{"issues": [{"number": 1, "title": "First"}]}</plan>'),
        _result_line('<plan>{"issues": [{"number": 2, "title": "Last"}]}</plan>'),
    ]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)
    assert result.issues == [{"number": 1, "title": "First"}]


def test_process_stream_implementer_stops_consuming_after_turn_with_commit_message():
    consumed: list[str] = []

    def tracking_iter():
        for line in [
            _assistant_line("<commit_message>done</commit_message>"),
            _assistant_line("This line must not be consumed"),
            _result_line("<commit_message>extra</commit_message>"),
        ]:
            consumed.append(line)
            yield line

    result = process_stream(
        tracking_iter(),
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
    )
    assert isinstance(result, CommitMessageOutput)
    assert result.message == "done"
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
            _result_line("<commit_message>done</commit_message>"),
            _assistant_line("This line must not be consumed"),
        ]:
            consumed.append(line)
            yield line

    result = process_stream(
        tracking_iter(),
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
    )
    assert isinstance(result, CommitMessageOutput)
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
        [_assistant_line("<commit_message>done</commit_message>")],
        on_turn=received.append,
        role=AgentRole.IMPLEMENTER,
    )
    assert received == ["<commit_message>done</commit_message>"]


def test_process_stream_non_error_result_with_pattern_text_does_not_raise():
    success_result = json.dumps(
        {
            "type": "result",
            "is_error": False,
            "result": "<commit_message>done</commit_message> usage limit in text",
        }
    )
    result = process_stream(
        [success_result],
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
    )
    assert isinstance(result, CommitMessageOutput)


# ── Token extraction via on_tokens callback ───────────────────────────────────


def test_process_stream_calls_on_tokens_with_input_tokens():
    token_counts: list[int] = []
    process_stream(
        [
            _assistant_line_with_usage("thinking", input_tokens=50_000),
            _result_line("<commit_message>done</commit_message>"),
        ],
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
        on_tokens=token_counts.append,
    )
    assert token_counts == [50_000]


def test_process_stream_on_tokens_sums_all_token_types():
    token_counts: list[int] = []
    process_stream(
        [
            _assistant_line_with_usage(
                "thinking",
                input_tokens=10_000,
                cache_creation=20_000,
                cache_read=30_000,
            ),
            _result_line("<commit_message>done</commit_message>"),
        ],
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
        on_tokens=token_counts.append,
    )
    assert token_counts == [60_000]


def test_process_stream_on_tokens_not_called_when_no_usage_block():
    token_counts: list[int] = []
    process_stream(
        [
            _assistant_line("no usage data"),
            _result_line("<commit_message>done</commit_message>"),
        ],
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
        on_tokens=token_counts.append,
    )
    assert token_counts == []


def test_process_stream_on_tokens_called_once_per_assistant_turn_with_usage():
    token_counts: list[int] = []
    process_stream(
        [
            _assistant_line_with_usage("first turn", input_tokens=10_000),
            _assistant_line_with_usage("second turn", input_tokens=20_000),
            _result_line("<commit_message>done</commit_message>"),
        ],
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
        on_tokens=token_counts.append,
    )
    assert token_counts == [10_000, 20_000]


def test_process_stream_on_tokens_is_optional():
    result = process_stream(
        [
            _assistant_line_with_usage("thinking", input_tokens=50_000),
            _result_line("<commit_message>done</commit_message>"),
        ],
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
    )
    assert isinstance(result, CommitMessageOutput)


def test_process_stream_improve_raises_when_no_promise_tag():
    lines = [_result_line("no promise tag here")]
    with pytest.raises(PromiseParseError):
        process_stream(lines, on_turn=lambda t: None, role=AgentRole.IMPROVE)


def test_process_stream_improve_returns_issue_output_for_json_issue_tag():
    lines = [
        _result_line(
            '<issue>{"number": 99, "labels": ["enhancement"]}</issue>'
            "<promise>COMPLETE</promise>"
        )
    ]
    result = process_stream(lines, on_turn=lambda t: None, role=AgentRole.IMPROVE)
    assert isinstance(result, IssueOutput)
    assert result.number == 99
    assert result.labels == ["enhancement"]


def test_process_stream_improve_json_issue_without_promise_raises():
    lines = [_result_line('<issue>{"number": 99, "labels": []}</issue>')]
    with pytest.raises(PromiseParseError):
        process_stream(lines, on_turn=lambda t: None, role=AgentRole.IMPROVE)


def test_process_stream_improve_json_issue_in_streaming_turn_returns_issue_output():
    consumed: list[str] = []

    def tracking_iter():
        for line in [
            _assistant_line(
                '<issue>{"number": 7, "labels": ["bug"]}</issue>'
                "<promise>COMPLETE</promise>"
            ),
            _assistant_line("This line must not be consumed"),
            _result_line("extra"),
        ]:
            consumed.append(line)
            yield line

    result = process_stream(
        tracking_iter(),
        on_turn=lambda t: None,
        role=AgentRole.IMPROVE,
    )
    assert isinstance(result, IssueOutput)
    assert result.number == 7
    assert len(consumed) == 1


# ── process_stream_from_events: fake AgentService driving the coordinator ─────


def test_process_stream_from_events_planner_driven_by_canned_events():
    events = [
        AssistantTurn(text='<plan>{"issues": [{"number": 1, "title": "Fix"}]}</plan>'),
        Result(text="done"),
    ]
    result = process_stream_from_events(
        iter(events), on_turn=lambda t: None, role=AgentRole.PLANNER
    )
    assert isinstance(result, PlannerOutput)
    assert result.issues == [{"number": 1, "title": "Fix"}]


def test_process_stream_from_events_implementer_returns_commit_message():
    events = [
        AssistantTurn(text="<commit_message>add feature</commit_message>"),
    ]
    result = process_stream_from_events(
        iter(events), on_turn=lambda t: None, role=AgentRole.IMPLEMENTER
    )
    assert isinstance(result, CommitMessageOutput)
    assert result.message == "add feature"


def test_extract_output_parses_planner_output():
    from pycastle.agents import output_protocol

    result = output_protocol.extract_output(
        text='<plan>{"issues": [{"number": 1, "title": "Fix bug"}]}</plan>',
        role=AgentRole.PLANNER,
    )
    assert isinstance(result, PlannerOutput)
    assert result.issues == [{"number": 1, "title": "Fix bug"}]


def test_extract_output_raises_for_missing_required_tags():
    from pycastle.agents import output_protocol

    with pytest.raises(AgentOutputProtocolError):
        output_protocol.extract_output(
            text="no tags",
            role=AgentRole.PLANNER,
        )


def test_extract_output_implementer_surfaces_behavior_output():
    from pycastle.agents import output_protocol

    result = output_protocol.extract_output(
        text=_behavior_block() + "\n<commit_message>add feature</commit_message>",
        role=AgentRole.IMPLEMENTER,
    )
    assert isinstance(result, CommitMessageOutput)
    assert result.message == "add feature"
    assert len(result.behaviors) == 1
    assert result.behaviors[0].name == "per-behavior emission"


def test_process_stream_still_importable_for_compatibility():
    result = process_stream(
        [_result_line("<commit_message>legacy stream path</commit_message>")],
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
    )
    assert isinstance(result, CommitMessageOutput)
    assert result.message == "legacy stream path"


# ── <behavior> tag — per-behavior emission ────────────────────────────────────


def _behavior_block(
    name: str = "per-behavior emission",
    observable_surface: str = "CommitMessageOutput carries N BehaviorOutput values",
    test_file: str = "tests/test_foo.py",
    failing_test_output: str = "FAILED tests/test_foo.py::test_thing - AssertionError",
) -> str:
    return (
        f"<behavior>\n"
        f"Behavior name: {name}\n"
        f"Observable surface: {observable_surface}\n"
        f"Test file: {test_file}\n"
        f"Failing test output:\n{failing_test_output}\n"
        f"</behavior>"
    )


def test_behavior_output_is_frozen():
    out = BehaviorOutput(
        name="x",
        observable_surface="y",
        test_file="tests/test_x.py",
        failing_test_output="FAILED",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.name = "z"  # type: ignore[misc]


def test_process_stream_implementer_surfaces_one_behavior_output():
    text = _behavior_block() + "\n<commit_message>add feature</commit_message>"
    result = process_stream(
        [_result_line(text)],
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
    )
    assert isinstance(result, CommitMessageOutput)
    assert len(result.behaviors) == 1
    b = result.behaviors[0]
    assert b.name == "per-behavior emission"
    assert "CommitMessageOutput" in b.observable_surface
    assert b.test_file == "tests/test_foo.py"
    assert "FAILED" in b.failing_test_output


def test_process_stream_implementer_surfaces_n_behavior_outputs():
    text = (
        _behavior_block(name="first behavior")
        + "\n"
        + _behavior_block(name="second behavior")
        + "\n<commit_message>add features</commit_message>"
    )
    result = process_stream(
        [_result_line(text)],
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
    )
    assert isinstance(result, CommitMessageOutput)
    assert len(result.behaviors) == 2
    assert result.behaviors[0].name == "first behavior"
    assert result.behaviors[1].name == "second behavior"


def test_process_stream_implementer_no_behavior_tags_succeeds():
    text = "<commit_message>add feature</commit_message>"
    result = process_stream(
        [_result_line(text)],
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
    )
    assert isinstance(result, CommitMessageOutput)
    assert result.behaviors == ()


def test_process_stream_implementer_collects_behaviors_from_earlier_turns():
    result = process_stream(
        [
            _assistant_line(_behavior_block(name="first behavior")),
            _assistant_line(
                _behavior_block(name="second behavior")
                + "\n<commit_message>done</commit_message>"
            ),
        ],
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
    )
    assert isinstance(result, CommitMessageOutput)
    assert len(result.behaviors) == 2
    assert result.behaviors[0].name == "first behavior"
    assert result.behaviors[1].name == "second behavior"


def test_process_stream_implementer_behaviors_in_result_envelope_only():
    text = (
        _behavior_block(name="env behavior") + "\n<commit_message>done</commit_message>"
    )
    result = process_stream(
        [_result_line(text)],
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
    )
    assert isinstance(result, CommitMessageOutput)
    assert len(result.behaviors) == 1
    assert result.behaviors[0].name == "env behavior"


def test_process_stream_from_events_implementer_no_behavior_tags_succeeds():
    events = [AssistantTurn(text="<commit_message>done</commit_message>")]
    result = process_stream_from_events(
        iter(events),
        on_turn=lambda t: None,
        role=AgentRole.IMPLEMENTER,
    )
    assert isinstance(result, CommitMessageOutput)
    assert result.behaviors == ()


# ── Reviewer uses CommitMessageHandler ────────────────────────────────────────


def test_reviewer_commit_message_extracted():
    result = process_stream(
        [_result_line("<commit_message>polish auth module</commit_message>")],
        on_turn=lambda t: None,
        role=AgentRole.REVIEWER,
    )
    assert isinstance(result, CommitMessageOutput)
    assert result.message == "polish auth module"


def test_reviewer_early_exit_on_commit_message_in_turn():
    result = process_stream(
        [_assistant_line("<commit_message>update readme</commit_message>")],
        on_turn=lambda t: None,
        role=AgentRole.REVIEWER,
    )
    assert isinstance(result, CommitMessageOutput)
    assert result.message == "update readme"
