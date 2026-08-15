import pytest

from pycastle.agents.output_protocol import (
    AgentOutputProtocolError,
    AgentRole,
    NoCandidateOutput,
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
