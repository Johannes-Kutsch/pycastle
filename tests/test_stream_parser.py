import json

import pytest

from pycastle.stream_parser import StreamParser


@pytest.fixture
def parser() -> StreamParser:
    return StreamParser()


# ── Tracer bullet: assistant text extraction ──────────────────────────────────


def test_feed_returns_text_from_assistant_turn(parser: StreamParser):
    line = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Analysing issues"}]},
        }
    )
    assert parser.feed(line) == "Analysing issues"


# ── system lines suppressed ───────────────────────────────────────────────────


def test_feed_returns_none_for_system_line(parser: StreamParser):
    line = json.dumps(
        {"type": "system", "subtype": "init", "session_id": "abc", "tools": []}
    )
    assert parser.feed(line) is None


# ── result lines suppressed ───────────────────────────────────────────────────


def test_feed_returns_none_for_result_line(parser: StreamParser):
    line = json.dumps({"type": "result", "result": "Final answer", "session_id": "abc"})
    assert parser.feed(line) is None


def test_feed_returns_none_for_empty_result_line(parser: StreamParser):
    line = json.dumps({"type": "result", "result": "", "session_id": "abc"})
    assert parser.feed(line) is None


# ── tool-use only turns suppressed ───────────────────────────────────────────


def test_feed_returns_none_for_tool_use_only_turn(parser: StreamParser):
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}
                ]
            },
        }
    )
    assert parser.feed(line) is None


def test_feed_drops_tool_use_blocks_keeps_text(parser: StreamParser):
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Reading files"},
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                ]
            },
        }
    )
    assert parser.feed(line) == "Reading files"


# ── multiple text blocks joined with \n\n ─────────────────────────────────────


def test_feed_joins_multiple_text_blocks_with_double_newline(parser: StreamParser):
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
    assert parser.feed(line) == "First block\n\nSecond block"


def test_feed_joins_three_text_blocks_with_double_newline(parser: StreamParser):
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Alpha"},
                    {"type": "text", "text": "Beta"},
                    {"type": "text", "text": "Gamma"},
                ]
            },
        }
    )
    assert parser.feed(line) == "Alpha\n\nBeta\n\nGamma"


# ── malformed JSON returns None without raising ───────────────────────────────


def test_feed_returns_none_for_malformed_json(parser: StreamParser):
    assert parser.feed("not valid json {{{") is None


def test_feed_returns_none_for_json_array(parser: StreamParser):
    assert parser.feed('["not", "a", "dict"]') is None


# ── sequential calls across multiple turns ────────────────────────────────────


def test_feed_sequential_turns_each_return_correct_text(parser: StreamParser):
    turn1 = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Turn one"}]},
        }
    )
    turn2 = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Turn two"}]},
        }
    )
    assert parser.feed(turn1) == "Turn one"
    assert parser.feed(turn2) == "Turn two"


def test_feed_mixed_sequence_returns_text_only_for_assistant_turns(
    parser: StreamParser,
):
    system_line = json.dumps({"type": "system", "subtype": "init"})
    assistant_line = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Hello"}]},
        }
    )
    result_line = json.dumps({"type": "result", "result": "done"})

    assert parser.feed(system_line) is None
    assert parser.feed(assistant_line) == "Hello"
    assert parser.feed(result_line) is None


# ── edge cases ────────────────────────────────────────────────────────────────


def test_feed_returns_none_for_whitespace_only_text(parser: StreamParser):
    line = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "   "}]},
        }
    )
    assert parser.feed(line) is None


def test_feed_returns_none_for_empty_content_list(parser: StreamParser):
    line = json.dumps({"type": "assistant", "message": {"content": []}})
    assert parser.feed(line) is None


def test_feed_returns_none_for_null_content(parser: StreamParser):
    line = json.dumps({"type": "assistant", "message": {"content": None}})
    assert parser.feed(line) is None


def test_feed_returns_none_for_missing_message(parser: StreamParser):
    line = json.dumps({"type": "assistant"})
    assert parser.feed(line) is None


def test_feed_returns_none_for_null_message(parser: StreamParser):
    line = json.dumps({"type": "assistant", "message": None})
    assert parser.feed(line) is None


def test_feed_returns_none_for_null_text_in_block(parser: StreamParser):
    line = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": None}]},
        }
    )
    assert parser.feed(line) is None


def test_feed_returns_none_for_unknown_type(parser: StreamParser):
    line = json.dumps({"type": "tool_result", "content": "output"})
    assert parser.feed(line) is None


def test_feed_preserves_multiline_text_within_block(parser: StreamParser):
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "First line\nSecond line\nThird line"}
                ]
            },
        }
    )
    assert parser.feed(line) == "First line\nSecond line\nThird line"
