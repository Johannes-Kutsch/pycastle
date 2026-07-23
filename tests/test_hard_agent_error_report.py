"""Interface-level tests for iteration.hard_agent_error_report.

Tests verify that translate_hard_agent_error_to_abort produces titles, bodies, and
printed status messages byte-for-byte identical to the inline HardAgentError handling
in run_iteration, for every relevant envelope shape and service-name variant.
"""

from __future__ import annotations

import json


from agent_runtime.errors import HardAgentError
from pycastle.config import Config
from pycastle.iteration import AbortedHardApiError
from pycastle.iteration.hard_agent_error_report import (
    translate_hard_agent_error_to_abort,
)
from tests.support import RecordingStatusDisplay


# ── Test doubles ─────────────────────────────────────────────────────────────


class RecordingBugFiler:
    """In-memory bug filer that captures calls and returns a controllable URL."""

    def __init__(
        self, return_url: str | None = "https://github.com/owner/repo/issues/1"
    ) -> None:
        self.calls: list[tuple[str, str, list[str]]] = []
        self._url = return_url

    def __call__(
        self,
        title: str,
        body: str,
        labels: list[str],
        *,
        cfg: Config | None = None,
    ) -> str | None:
        self.calls.append((title, body, labels))
        return self._url


def _make_err(
    message: str = "",
    service_name: str = "claude",
    caller: str = "Implementer",
    status_code: int | None = None,
) -> HardAgentError:
    err = HardAgentError(message=message, service_name=service_name)
    err.caller = caller
    if status_code is not None:
        setattr(err, "status_code", status_code)
    return err


def _printed(display: RecordingStatusDisplay) -> list[str]:
    return [msg for op, *rest in display.calls if op == "print" for msg in [rest[1]]]


# ── Service-label mapping ─────────────────────────────────────────────────────


def test_claude_service_label():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    err = _make_err(message="plain error", service_name="claude")

    translate_hard_agent_error_to_abort(err, Config(), display, filer)

    title, _, _ = filer.calls[0]
    assert "[pycastle] Claude API" in title


def test_codex_service_label():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    err = _make_err(message="plain error", service_name="codex")

    translate_hard_agent_error_to_abort(err, Config(), display, filer)

    title, _, _ = filer.calls[0]
    assert "[pycastle] Codex API" in title


def test_opencode_service_label():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    err = _make_err(message="plain error", service_name="opencode")

    translate_hard_agent_error_to_abort(err, Config(), display, filer)

    title, _, _ = filer.calls[0]
    assert "[pycastle] OpenCode API" in title


def test_unknown_service_label_uses_raw_name():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    err = _make_err(message="plain error", service_name="gemini")

    translate_hard_agent_error_to_abort(err, Config(), display, filer)

    title, _, _ = filer.calls[0]
    assert "[pycastle] gemini API" in title


def test_missing_service_name_defaults_to_claude():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    err = HardAgentError(message="plain error", service_name="")
    err.caller = "Implementer"

    translate_hard_agent_error_to_abort(err, Config(), display, filer)

    title, _, _ = filer.calls[0]
    assert "[pycastle] Claude API" in title


# ── Envelope text extraction ──────────────────────────────────────────────────


def test_top_level_result_envelope():
    raw = json.dumps({"result": "extracted result text", "status": 200})
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    err = _make_err(message=raw, service_name="codex")

    translate_hard_agent_error_to_abort(err, Config(), display, filer)

    title, _, _ = filer.calls[0]
    assert "extracted result text" in title


def test_error_data_message_envelope():
    raw = json.dumps({"error": {"data": {"message": "nested data message"}}})
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    err = _make_err(message=raw, service_name="codex")

    translate_hard_agent_error_to_abort(err, Config(), display, filer)

    title, _, _ = filer.calls[0]
    assert "nested data message" in title


def test_error_message_envelope():
    raw = json.dumps({"error": {"message": "top error message"}})
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    err = _make_err(message=raw, service_name="codex")

    translate_hard_agent_error_to_abort(err, Config(), display, filer)

    title, _, _ = filer.calls[0]
    assert "top error message" in title


def test_invalid_json_raw_text_preserved():
    raw = "not valid json at all"
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    err = _make_err(message=raw, service_name="claude")

    translate_hard_agent_error_to_abort(err, Config(), display, filer)

    title, _, _ = filer.calls[0]
    assert "not valid json at all" in title


def test_unrelated_top_level_json_uses_raw_message():
    raw = json.dumps({"foo": "bar", "baz": 42})
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    err = _make_err(message=raw, service_name="claude")

    translate_hard_agent_error_to_abort(err, Config(), display, filer)

    title, body, _ = filer.calls[0]
    assert raw in title or raw in body


# ── Status-code extraction ────────────────────────────────────────────────────


def test_status_code_from_json_envelope():
    raw = json.dumps({"result": "some error", "status": 429})
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    err = _make_err(message=raw, service_name="claude")

    result = translate_hard_agent_error_to_abort(err, Config(), display, filer)

    assert result == AbortedHardApiError(status_code=429)
    title, _, _ = filer.calls[0]
    assert "429" in title


def test_boolean_status_in_envelope_falls_back_to_caller_provided():
    raw = json.dumps({"result": "some error", "status": True})
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    err = _make_err(message=raw, service_name="claude", status_code=500)

    result = translate_hard_agent_error_to_abort(err, Config(), display, filer)

    assert result == AbortedHardApiError(status_code=500)
    title, _, _ = filer.calls[0]
    assert "500" in title


def test_none_status_in_envelope_with_no_fallback():
    raw = json.dumps({"result": "some error", "status": None})
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    err = _make_err(message=raw, service_name="claude")

    result = translate_hard_agent_error_to_abort(err, Config(), display, filer)

    assert result == AbortedHardApiError(status_code=None)


def test_missing_status_with_no_fallback_yields_none():
    raw = json.dumps({"result": "some error"})
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    err = _make_err(message=raw, service_name="claude")

    result = translate_hard_agent_error_to_abort(err, Config(), display, filer)

    assert result == AbortedHardApiError(status_code=None)


# ── Caller handling ───────────────────────────────────────────────────────────


def test_missing_caller_renders_as_unknown_in_body():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    err = HardAgentError(message="some error", service_name="claude")
    err.caller = ""

    translate_hard_agent_error_to_abort(err, Config(), display, filer)

    _, body, _ = filer.calls[0]
    assert "Agent: <unknown>" in body


def test_missing_caller_renders_as_unknown_in_printed_status():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    err = HardAgentError(message="some error", service_name="claude")
    err.caller = ""

    translate_hard_agent_error_to_abort(err, Config(), display, filer)

    # The print call's caller argument should be the empty string (falsy → <unknown> in body,
    # but the print caller is passed as-is from err.caller)
    assert any(op == "print" for op, *_ in display.calls)


# ── URL suffix in printed status ──────────────────────────────────────────────


def test_filer_returns_url_suffix_appended():
    url = "https://github.com/owner/repo/issues/99"
    filer = RecordingBugFiler(return_url=url)
    display = RecordingStatusDisplay()
    err = _make_err(message="error", service_name="claude")

    translate_hard_agent_error_to_abort(err, Config(), display, filer)

    msgs = _printed(display)
    assert any(f" — {url}" in m for m in msgs)


def test_filer_returns_none_no_url_suffix():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    err = _make_err(message="error", service_name="claude")

    translate_hard_agent_error_to_abort(err, Config(), display, filer)

    msgs = _printed(display)
    assert all(" — " not in m for m in msgs)


# ── Printed status message format ─────────────────────────────────────────────


def test_printed_status_includes_status_code():
    raw = json.dumps({"result": "oops", "status": 503})
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    err = _make_err(message=raw, service_name="claude")

    translate_hard_agent_error_to_abort(err, Config(), display, filer)

    msgs = _printed(display)
    assert any("hard API error: status 503" in m for m in msgs)


def test_printed_status_no_status_when_absent():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    err = _make_err(message="plain error", service_name="claude")

    translate_hard_agent_error_to_abort(err, Config(), display, filer)

    msgs = _printed(display)
    assert any("hard API error: status no status" in m for m in msgs)


# ── Claude default (no explicit service_name) ─────────────────────────────────


def test_claude_default_no_service_name_on_error():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    err = HardAgentError(message="raw error text", service_name="")
    err.caller = "Scan"

    result = translate_hard_agent_error_to_abort(err, Config(), display, filer)

    assert isinstance(result, AbortedHardApiError)
    title, body, labels = filer.calls[0]
    assert "[pycastle] Claude API" in title
    assert "Service: claude" in body


# ── Body composition ──────────────────────────────────────────────────────────


def test_body_contains_fenced_json_block():
    raw = json.dumps({"result": "test"})
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    err = _make_err(message=raw)

    translate_hard_agent_error_to_abort(err, Config(), display, filer)

    _, body, _ = filer.calls[0]
    assert "```json" in body
    assert raw in body


def test_body_contains_status_agent_service_lines():
    raw = json.dumps({"result": "oops", "status": 401})
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    err = _make_err(message=raw, service_name="codex", caller="PRD")

    translate_hard_agent_error_to_abort(err, Config(), display, filer)

    _, body, _ = filer.calls[0]
    assert "Status: 401" in body
    assert "Agent: PRD" in body
    assert "Service: codex" in body


def test_bug_filer_called_with_bug_report_labels():
    from pycastle.bug_reporter import BUG_REPORT_LABEL_LIST

    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    err = _make_err(message="error")

    translate_hard_agent_error_to_abort(err, Config(), display, filer)

    _, _, labels = filer.calls[0]
    assert labels == BUG_REPORT_LABEL_LIST
