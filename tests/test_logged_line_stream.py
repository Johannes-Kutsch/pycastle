import json
import threading
from pathlib import Path

import pytest

from pycastle.errors import AgentTimeoutError
from pycastle.infrastructure._logged_line_stream import stream_logged_lines


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
    assert json.loads(log_lines[1]) == {
        "type": "pycastle_input",
        "prompt": "prompt",
    }
    assert log_lines[2] == "next line"


def test_stream_logged_lines_yields_partial_final_line_without_newline(tmp_path):
    log_path = tmp_path / "agent.log"

    lines, on_chunk_calls = _collect_stream([b"partial"], log_path=log_path)

    assert lines == ["partial"]
    assert on_chunk_calls == ["chunk"]


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
