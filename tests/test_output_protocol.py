import pytest

from pycastle.agents.output_protocol import (
    AgentOutputProtocolError,
    AgentRole,
    CommitMessageOutput,
    CompletionOutput,
    FailedOutput,
    IssueOutput,
    IssueParseError,
    NoCandidateOutput,
    PlannerOutput,
    PlanParseError,
    PromiseParseError,
    ScanCandidateItem,
    ScanCandidatesOutput,
    extract_output,
)

# ── Behavior 1: valid candidates block yields ordered list ────────────────────


def test_scan_with_valid_candidates_yields_scan_candidates_output():
    text = """
<candidates>[{"rank": 1, "title": "First", "summary": "A summary"}, {"rank": 2, "title": "Second"}]</candidates>
<promise>COMPLETE</promise>
"""
    result = extract_output(text, AgentRole.IMPROVE)
    assert isinstance(result, ScanCandidatesOutput)


def test_scan_candidates_carry_rank_title_and_optional_summary():
    text = """
<candidates>[{"rank": 1, "title": "Refactor seam", "summary": "Deepen module X"}, {"rank": 2, "title": "Type tightening"}]</candidates>
<promise>COMPLETE</promise>
"""
    result = extract_output(text, AgentRole.IMPROVE)
    assert isinstance(result, ScanCandidatesOutput)
    assert result.candidates[0] == ScanCandidateItem(
        rank=1, title="Refactor seam", summary="Deepen module X"
    )
    assert result.candidates[1] == ScanCandidateItem(
        rank=2, title="Type tightening", summary=None
    )


def test_scan_candidates_preserve_rank_order():
    text = """
<candidates>[{"rank": 1, "title": "Best"}, {"rank": 2, "title": "Runner-up"}, {"rank": 3, "title": "Third"}]</candidates>
<promise>COMPLETE</promise>
"""
    result = extract_output(text, AgentRole.IMPROVE)
    assert isinstance(result, ScanCandidatesOutput)
    assert [c.rank for c in result.candidates] == [1, 2, 3]


def test_scan_fewer_candidates_than_max_yields_exactly_what_was_returned():
    text = """
<candidates>[{"rank": 1, "title": "Only one"}]</candidates>
<promise>COMPLETE</promise>
"""
    result = extract_output(text, AgentRole.IMPROVE)
    assert isinstance(result, ScanCandidatesOutput)
    assert len(result.candidates) == 1
    assert result.candidates[0].rank == 1
    assert result.candidates[0].title == "Only one"


# ── Behavior 2: no-candidate promise → existing no-candidate path ─────────────


def test_scan_no_candidate_promise_yields_no_candidate_output():
    text = "<promise>NO-CANDIDATE</promise>"
    result = extract_output(text, AgentRole.IMPROVE)
    assert isinstance(result, NoCandidateOutput)


# ── Behavior 3: malformed candidates block raises AgentOutputProtocolError ────


def test_scan_malformed_candidates_json_raises_protocol_error():
    text = """<candidates>not-valid-json</candidates>
<promise>COMPLETE</promise>
"""
    with pytest.raises(AgentOutputProtocolError):
        extract_output(text, AgentRole.IMPROVE)


def test_scan_candidates_missing_rank_raises_protocol_error():
    text = """<candidates>[{"title": "No rank here"}]</candidates>
<promise>COMPLETE</promise>
"""
    with pytest.raises(AgentOutputProtocolError):
        extract_output(text, AgentRole.IMPROVE)


def test_scan_candidates_missing_title_raises_protocol_error():
    text = """<candidates>[{"rank": 1}]</candidates>
<promise>COMPLETE</promise>
"""
    with pytest.raises(AgentOutputProtocolError):
        extract_output(text, AgentRole.IMPROVE)


def test_scan_empty_candidates_array_raises_protocol_error():
    text = """<candidates>[]</candidates>
<promise>COMPLETE</promise>
"""
    with pytest.raises(AgentOutputProtocolError):
        extract_output(text, AgentRole.IMPROVE)


# ── PLANNER: success paths ─────────────────────────────────────────────────────


def test_planner_issues_key_returns_planner_output_with_parsed_issues():
    text = '<plan>{"issues": [{"number": 1, "title": "First"}, {"number": 2, "title": "Second"}]}</plan>'
    result = extract_output(text, AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)
    assert result.issues == [
        {"number": 1, "title": "First"},
        {"number": 2, "title": "Second"},
    ]


def test_planner_unblocked_issues_alias_returns_planner_output():
    text = '<plan>{"unblocked_issues": [{"number": 3, "title": "Third"}]}</plan>'
    result = extract_output(text, AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)
    assert result.issues == [{"number": 3, "title": "Third"}]


def test_planner_blocked_entries_are_carried_in_blocked_field():
    text = '<plan>{"issues": [{"number": 1, "title": "Issue"}], "blocked": [{"number": 5, "title": "Blocked"}]}</plan>'
    result = extract_output(text, AgentRole.PLANNER)
    assert isinstance(result, PlannerOutput)
    assert result.blocked == [{"number": 5, "title": "Blocked"}]


# ── PLANNER: error paths ───────────────────────────────────────────────────────


def test_planner_malformed_json_raises_plan_parse_error():
    text = "<plan>not-valid-json</plan>"
    with pytest.raises(PlanParseError):
        extract_output(text, AgentRole.PLANNER)


def test_planner_missing_issues_key_raises_plan_parse_error():
    text = '<plan>{"other_key": []}</plan>'
    with pytest.raises(PlanParseError):
        extract_output(text, AgentRole.PLANNER)


def test_planner_issue_missing_number_raises_plan_parse_error():
    text = '<plan>{"issues": [{"title": "No number"}]}</plan>'
    with pytest.raises(PlanParseError):
        extract_output(text, AgentRole.PLANNER)


def test_planner_issue_missing_title_raises_plan_parse_error():
    text = '<plan>{"issues": [{"number": 1}]}</plan>'
    with pytest.raises(PlanParseError):
        extract_output(text, AgentRole.PLANNER)


# ── PREFLIGHT_ISSUE / FAILURE_REPORT: success paths ───────────────────────────


def test_preflight_issue_valid_body_returns_issue_output_with_number_and_labels():
    text = '<issue>{"number": 42, "labels": ["bug", "ready-for-agent"]}</issue>'
    result = extract_output(text, AgentRole.PREFLIGHT_ISSUE)
    assert isinstance(result, IssueOutput)
    assert result.number == 42
    assert result.labels == ["bug", "ready-for-agent"]


def test_failure_report_valid_body_returns_issue_output():
    text = '<issue>{"number": 7, "labels": ["failure"]}</issue>'
    result = extract_output(text, AgentRole.FAILURE_REPORT)
    assert isinstance(result, IssueOutput)
    assert result.number == 7
    assert result.labels == ["failure"]


# ── PREFLIGHT_ISSUE / FAILURE_REPORT: error paths ─────────────────────────────


def test_preflight_issue_malformed_json_raises_issue_parse_error():
    text = "<issue>not-json</issue>"
    with pytest.raises(IssueParseError):
        extract_output(text, AgentRole.PREFLIGHT_ISSUE)


def test_preflight_issue_missing_number_raises_issue_parse_error():
    text = '<issue>{"labels": ["bug"]}</issue>'
    with pytest.raises(IssueParseError):
        extract_output(text, AgentRole.PREFLIGHT_ISSUE)


def test_preflight_issue_missing_labels_raises_issue_parse_error():
    text = '<issue>{"number": 1}</issue>'
    with pytest.raises(IssueParseError):
        extract_output(text, AgentRole.PREFLIGHT_ISSUE)


def test_preflight_issue_no_tag_raises_issue_parse_error():
    text = "no issue tag here"
    with pytest.raises(IssueParseError):
        extract_output(text, AgentRole.PREFLIGHT_ISSUE)


def test_failure_report_malformed_json_raises_issue_parse_error():
    text = "<issue>bad</issue>"
    with pytest.raises(IssueParseError):
        extract_output(text, AgentRole.FAILURE_REPORT)


# ── IMPLEMENTER / REVIEWER / MERGER: success paths ────────────────────────────


def test_implementer_with_commit_message_returns_commit_message_output():
    text = "<commit_message>Add feature X</commit_message>"
    result = extract_output(text, AgentRole.IMPLEMENTER)
    assert isinstance(result, CommitMessageOutput)
    assert result.message == "Add feature X"


def test_reviewer_with_commit_message_returns_commit_message_output():
    text = "<commit_message>Review changes</commit_message>"
    result = extract_output(text, AgentRole.REVIEWER)
    assert isinstance(result, CommitMessageOutput)
    assert result.message == "Review changes"


def test_merger_with_commit_message_returns_commit_message_output():
    text = "<commit_message>Merge branch</commit_message>"
    result = extract_output(text, AgentRole.MERGER)
    assert isinstance(result, CommitMessageOutput)
    assert result.message == "Merge branch"


def test_implementer_without_commit_message_returns_commit_message_output_with_none():
    text = "no commit message here"
    result = extract_output(text, AgentRole.IMPLEMENTER)
    assert isinstance(result, CommitMessageOutput)
    assert result.message is None


def test_reviewer_without_commit_message_returns_commit_message_output_with_none():
    text = "no commit message here"
    result = extract_output(text, AgentRole.REVIEWER)
    assert isinstance(result, CommitMessageOutput)
    assert result.message is None


def test_merger_without_commit_message_returns_commit_message_output_with_none():
    text = "no commit message here"
    result = extract_output(text, AgentRole.MERGER)
    assert isinstance(result, CommitMessageOutput)
    assert result.message is None


# ── CommitMessageOutput: behaviors ────────────────────────────────────────────


def test_commit_message_output_single_behavior_block_is_parsed():
    text = """\
<commit_message>Fix bug</commit_message>
<behavior>
Behavior name: test-behavior
Observable surface: cli
Test file: tests/test_foo.py
Failing test output:
FAILED tests/test_foo.py::test_bar
</behavior>
"""
    result = extract_output(text, AgentRole.IMPLEMENTER)
    assert isinstance(result, CommitMessageOutput)
    assert len(result.behaviors) == 1
    b = result.behaviors[0]
    assert b.name == "test-behavior"
    assert b.observable_surface == "cli"
    assert b.test_file == "tests/test_foo.py"
    assert b.failing_test_output == "FAILED tests/test_foo.py::test_bar"


def test_commit_message_output_multiple_behavior_blocks_all_captured():
    text = """\
<commit_message>Improve X</commit_message>
<behavior>
Behavior name: first
Observable surface: api
Test file: tests/test_a.py
Failing test output:
FAILED test_a
</behavior>
<behavior>
Behavior name: second
Observable surface: db
Test file: tests/test_b.py
Failing test output:
FAILED test_b
</behavior>
"""
    result = extract_output(text, AgentRole.REVIEWER)
    assert isinstance(result, CommitMessageOutput)
    assert len(result.behaviors) == 2
    assert result.behaviors[0].name == "first"
    assert result.behaviors[1].name == "second"


def test_commit_message_output_no_behavior_blocks_yields_empty_tuple():
    text = "<commit_message>Simple commit</commit_message>"
    result = extract_output(text, AgentRole.IMPLEMENTER)
    assert isinstance(result, CommitMessageOutput)
    assert result.behaviors == ()


# ── DIVERGENCE_RESOLVER ────────────────────────────────────────────────────────


def test_divergence_resolver_complete_returns_completion_output():
    text = "<promise>COMPLETE</promise>"
    result = extract_output(text, AgentRole.DIVERGENCE_RESOLVER)
    assert isinstance(result, CompletionOutput)


def test_divergence_resolver_failed_returns_failed_output():
    text = "<promise>FAILED</promise>"
    result = extract_output(text, AgentRole.DIVERGENCE_RESOLVER)
    assert isinstance(result, FailedOutput)


def test_divergence_resolver_no_promise_raises_promise_parse_error():
    text = "no promise here"
    with pytest.raises(PromiseParseError):
        extract_output(text, AgentRole.DIVERGENCE_RESOLVER)


# ── IMPROVE: ticket phases ─────────────────────────────────────────────────────


def test_improve_bare_integer_issue_tags_return_completion_output_with_issue_numbers():
    text = "Filed <issue>42</issue> and <issue>43</issue>\n<promise>COMPLETE</promise>"
    result = extract_output(text, AgentRole.IMPROVE)
    assert isinstance(result, CompletionOutput)
    assert result.issue_numbers == (42, 43)


def test_improve_no_issue_tags_returns_completion_output_with_empty_issue_numbers():
    text = "<promise>COMPLETE</promise>"
    result = extract_output(text, AgentRole.IMPROVE)
    assert isinstance(result, CompletionOutput)
    assert result.issue_numbers == ()


def test_improve_spec_phase_json_issue_block_returns_issue_output():
    text = '<issue>{"number": 100, "labels": ["improvement"]}</issue>\n<promise>COMPLETE</promise>'
    result = extract_output(text, AgentRole.IMPROVE)
    assert isinstance(result, IssueOutput)
    assert result.number == 100


def test_improve_no_promise_raises_agent_output_protocol_error():
    text = "no promise here"
    with pytest.raises(AgentOutputProtocolError):
        extract_output(text, AgentRole.IMPROVE)


def test_improve_failed_promise_returns_failed_output():
    text = "<promise>FAILED</promise>"
    result = extract_output(text, AgentRole.IMPROVE)
    assert isinstance(result, FailedOutput)


def test_improve_no_candidate_promise_returns_no_candidate_output():
    text = "<promise>NO-CANDIDATE</promise>"
    result = extract_output(text, AgentRole.IMPROVE)
    assert isinstance(result, NoCandidateOutput)
