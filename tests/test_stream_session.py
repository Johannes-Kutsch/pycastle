import json
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from pycastle.agent_output_protocol import AgentRole, CompletionOutput, PlannerOutput
from pycastle.errors import AgentTimeoutError, UsageLimitError
from pycastle.stream_session import WorkStream


def _result_line(content: str) -> bytes:
    return (
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": content,
                "is_error": False,
            }
        ).encode()
        + b"\n"
    )


def _assistant_line(text: str) -> bytes:
    return (
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": text}]},
            }
        ).encode()
        + b"\n"
    )


def _usage_limit_line() -> bytes:
    return (
        json.dumps(
            {
                "type": "result",
                "is_error": True,
                "api_error_status": 429,
                "result": "rate limit",
            }
        ).encode()
        + b"\n"
    )


def _noop(turn: str) -> None:
    pass


def _noop_chunk() -> None:
    pass


def _make_workstream(
    chunks,
    tmp_path: Path,
    idle_timeout: float = 5.0,
    on_chunk: Callable[[], None] | None = None,
) -> WorkStream:
    return WorkStream(
        chunks=chunks,
        log_path=tmp_path / "test.log",
        idle_timeout=idle_timeout,
        on_chunk=on_chunk if on_chunk is not None else _noop_chunk,
    )


# ── Normal streaming ──────────────────────────────────────────────────────────


def test_implementer_stream_returns_completion_output(tmp_path):
    chunks = [_result_line("<promise>COMPLETE</promise>")]
    ws = _make_workstream(chunks, tmp_path)
    result = ws.run(AgentRole.IMPLEMENTER, _noop)
    assert isinstance(result, CompletionOutput)


def test_reviewer_stream_returns_completion_output(tmp_path):
    chunks = [_result_line("<promise>COMPLETE</promise>")]
    ws = _make_workstream(chunks, tmp_path)
    result = ws.run(AgentRole.REVIEWER, _noop)
    assert isinstance(result, CompletionOutput)


def test_planner_stream_returns_planner_output(tmp_path):
    issues = [{"number": 1, "title": "fix it"}]
    plan_json = json.dumps({"issues": issues})
    chunks = [_assistant_line(f"<plan>{plan_json}</plan>")]
    ws = _make_workstream(chunks, tmp_path)
    result = ws.run(AgentRole.PLANNER, _noop)
    assert isinstance(result, PlannerOutput)
    assert result.issues == issues


# ── Idle timeout ──────────────────────────────────────────────────────────────


def test_idle_timeout_raises_agent_timeout_error(tmp_path):
    event = threading.Event()

    def stalled():
        event.wait()
        yield b"never"

    ws = _make_workstream(stalled(), tmp_path, idle_timeout=0.05)
    with pytest.raises(AgentTimeoutError):
        ws.run(AgentRole.IMPLEMENTER, _noop)


# ── Log file ──────────────────────────────────────────────────────────────────


def test_log_file_contains_all_chunk_bytes(tmp_path):
    chunk1 = (
        b'{"type":"result","result":"<promise>COMPLETE</promise>","is_error":false}'
    )
    chunk2 = b"\n"
    ws = _make_workstream([chunk1, chunk2], tmp_path)
    ws.run(AgentRole.IMPLEMENTER, _noop)
    assert (tmp_path / "test.log").read_bytes() == chunk1 + chunk2


# ── on_chunk callback ─────────────────────────────────────────────────────────


def test_on_chunk_fires_once_per_chunk(tmp_path):
    call_count = [0]

    def count_call():
        call_count[0] += 1

    chunks = [
        b'{"type":"result","result":"<promise>COMPLETE</promise>","is_error":false}',
        b"\n",
    ]
    ws = _make_workstream(chunks, tmp_path, on_chunk=count_call)
    ws.run(AgentRole.IMPLEMENTER, _noop)
    assert call_count[0] == 2


# ── Line splitting across chunk boundaries ────────────────────────────────────


def test_lines_split_across_chunk_boundaries_are_assembled(tmp_path):
    full_line = (
        b'{"type":"result","result":"<promise>COMPLETE</promise>","is_error":false}\n'
    )
    mid = len(full_line) // 2
    chunks = [full_line[:mid], full_line[mid:]]
    ws = _make_workstream(chunks, tmp_path)
    result = ws.run(AgentRole.IMPLEMENTER, _noop)
    assert isinstance(result, CompletionOutput)


# ── Usage limit ───────────────────────────────────────────────────────────────


def test_usage_limit_chunk_raises_usage_limit_error(tmp_path):
    chunks = [_usage_limit_line()]
    ws = _make_workstream(chunks, tmp_path)
    with pytest.raises(UsageLimitError):
        ws.run(AgentRole.IMPLEMENTER, _noop)


# ── Partial final line (no trailing newline) ──────────────────────────────────


def test_result_line_without_trailing_newline_is_processed(tmp_path):
    line_bytes = (
        b'{"type":"result","result":"<promise>COMPLETE</promise>","is_error":false}'
    )
    ws = _make_workstream([line_bytes], tmp_path)
    result = ws.run(AgentRole.IMPLEMENTER, _noop)
    assert isinstance(result, CompletionOutput)


# ── Multi-newline chunk ───────────────────────────────────────────────────────


def test_chunk_containing_multiple_newlines_yields_all_lines(tmp_path):
    line1 = (
        b'{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}\n'
    )
    line2 = (
        b'{"type":"result","result":"<promise>COMPLETE</promise>","is_error":false}\n'
    )
    ws = _make_workstream([line1 + line2], tmp_path)
    turns: list[str] = []
    result = ws.run(AgentRole.IMPLEMENTER, turns.append)
    assert isinstance(result, CompletionOutput)
    assert turns == ["hello"]
