import json
import threading
from pathlib import Path

import pytest

from pycastle.errors import AgentTimeoutError
from pycastle.agents.output_protocol import AgentRole
from pycastle.infrastructure._logged_line_stream import (
    stream_logged_lines,
    stream_logged_work_lines,
)
from pycastle.session.resume import RunKind


def _collect_stream(
    chunks, *, log_path: Path, idle_timeout: float = 1.0, input_record=None
):
    on_chunk_calls: list[str] = []
    lines = list(
        stream_logged_lines(
            chunks,
            log_path=log_path,
            input_record=input_record
            or {
                "type": "pycastle_input",
                "prompt": "prompt",
            },
            idle_timeout=idle_timeout,
            on_chunk=lambda: on_chunk_calls.append("chunk"),
        )
    )
    return lines, on_chunk_calls


def test_stream_logged_work_lines_handles_one_complete_invocation(tmp_path):
    log_path = tmp_path / "agent.log"
    on_chunk_calls: list[str] = []

    lines = list(
        stream_logged_work_lines(
            [b'{"type":"result","result":"done"}\n'],
            log_path=log_path,
            role=AgentRole.IMPLEMENTER,
            run_kind=RunKind.FRESH,
            session_uuid=None,
            prompt="prompt",
            idle_timeout=1.0,
            on_chunk=lambda: on_chunk_calls.append("chunk"),
        )
    )

    assert lines == ['{"type":"result","result":"done"}']
    assert on_chunk_calls == ["chunk"]
    assert log_path.read_bytes() == (
        b'{"type": "pycastle_input", "role": "implementer", "run_kind": "fresh", '
        b'"session_uuid": null, "prompt": "prompt"}\n'
        b'{"type":"result","result":"done"}\n'
    )


def test_stream_logged_work_lines_repeated_invocations_insert_one_blank_line_separator(
    tmp_path,
):
    log_path = tmp_path / "agent.log"

    list(
        stream_logged_work_lines(
            [b'{"type":"result","result":"first"}'],
            log_path=log_path,
            role=AgentRole.IMPLEMENTER,
            run_kind=RunKind.FRESH,
            session_uuid="session-1",
            prompt="first prompt",
            idle_timeout=1.0,
            on_chunk=lambda: None,
        )
    )

    list(
        stream_logged_work_lines(
            [b'{"type":"result","result":"second"}\n'],
            log_path=log_path,
            role=AgentRole.REVIEWER,
            run_kind=RunKind.RESUME,
            session_uuid="session-2",
            prompt="second prompt",
            idle_timeout=1.0,
            on_chunk=lambda: None,
        )
    )

    log_lines = log_path.read_text(encoding="utf-8").splitlines()

    assert json.loads(log_lines[0]) == {
        "type": "pycastle_input",
        "role": "implementer",
        "run_kind": "fresh",
        "session_uuid": "session-1",
        "prompt": "first prompt",
    }
    assert log_lines[1] == '{"type":"result","result":"first"}'
    assert log_lines[2] == ""
    assert json.loads(log_lines[3]) == {
        "type": "pycastle_input",
        "role": "reviewer",
        "run_kind": "resume",
        "session_uuid": "session-2",
        "prompt": "second prompt",
    }
    assert log_lines[4] == '{"type":"result","result":"second"}'


def test_stream_logged_lines_logs_input_record_and_chunk_bytes(tmp_path):
    log_path = tmp_path / "agent.log"
    chunks = [b'{"type":"result"', b',"result":"done"}\n']

    lines, on_chunk_calls = _collect_stream(chunks, log_path=log_path)

    assert lines == ['{"type":"result","result":"done"}']
    assert on_chunk_calls == ["chunk", "chunk"]
    header, rest = log_path.read_bytes().split(b"\n", 1)
    assert json.loads(header) == {
        "type": "pycastle_input",
        "prompt": "prompt",
    }
    assert rest == b"".join(chunks)


def test_stream_logged_lines_appends_new_record_after_blank_separator(tmp_path):
    log_path = tmp_path / "agent.log"
    log_path.write_bytes(b"previous line")

    lines, _ = _collect_stream([b"next line\n"], log_path=log_path)

    assert lines == ["next line"]
    log_bytes = log_path.read_bytes()
    assert log_bytes.startswith(b"previous line\n")
    log_lines = log_path.read_text(encoding="utf-8").split("\n")
    assert log_lines[0] == "previous line"
    assert log_lines[1] == ""
    assert json.loads(log_lines[2]) == {
        "type": "pycastle_input",
        "prompt": "prompt",
    }
    assert log_lines[3] == "next line"


def test_stream_logged_lines_yields_partial_final_line_without_newline(tmp_path):
    log_path = tmp_path / "agent.log"

    lines, on_chunk_calls = _collect_stream([b"partial"], log_path=log_path)

    assert lines == ["partial"]
    assert on_chunk_calls == ["chunk"]


def test_stream_logged_lines_yields_each_line_from_one_multiline_chunk_in_order(
    tmp_path,
):
    log_path = tmp_path / "agent.log"

    lines, on_chunk_calls = _collect_stream(
        [b"first line\nsecond line\nthird line\n"],
        log_path=log_path,
    )

    assert lines == ["first line", "second line", "third line"]
    assert on_chunk_calls == ["chunk"]


def test_stream_logged_lines_waits_for_terminating_newline_before_yielding_split_line(
    tmp_path,
):
    log_path = tmp_path / "agent.log"
    chunks = [b"split", b" line", b"\nnext line\n"]

    lines, on_chunk_calls = _collect_stream(chunks, log_path=log_path)

    assert lines == ["split line", "next line"]
    assert on_chunk_calls == ["chunk", "chunk", "chunk"]


def test_stream_logged_lines_decodes_split_utf8_sequences_with_replacement_and_preserves_log_bytes(
    tmp_path,
):
    log_path = tmp_path / "agent.log"
    chunks = [b"\xf0\x9f", b"\x98\x80\nbad:\xff\n"]

    lines, on_chunk_calls = _collect_stream(chunks, log_path=log_path)

    assert lines == ["😀", "bad:\ufffd"]
    assert on_chunk_calls == ["chunk", "chunk"]
    _header, rest = log_path.read_bytes().split(b"\n", 1)
    assert rest == b"".join(chunks)


def test_stream_logged_lines_raises_agent_timeout_error_after_idle_timeout(tmp_path):
    log_path = tmp_path / "agent.log"
    event = threading.Event()

    chunks = (event.wait() or b"never" for _ in range(1))

    with pytest.raises(AgentTimeoutError, match="Agent idle for more than 0.05s"):
        list(
            stream_logged_lines(
                chunks,
                log_path=log_path,
                input_record={"type": "pycastle_input", "prompt": "stalled"},
                idle_timeout=0.05,
                on_chunk=lambda: None,
            )
        )

    first_line = log_path.read_bytes().split(b"\n")[0]
    assert json.loads(first_line) == {
        "type": "pycastle_input",
        "prompt": "stalled",
    }
