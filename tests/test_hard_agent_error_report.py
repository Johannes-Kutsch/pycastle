"""Interface-level tests for iteration.hard_agent_error_report.

Composition coverage (title, body, labels, service-label mapping, envelope
extraction) lives in test_upstream_issue_report.py.  These tests cover the
translator-level concerns: status-print format, returned AbortedHardApiError
status_code, and never-raise behaviour when auto_file_issue returns no url.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from agent_runtime.errors import HardAgentError

from pycastle.config import Config
from pycastle.iteration import AbortedHardApiError
from pycastle.iteration.hard_agent_error_report import (
    translate_hard_agent_error_to_abort,
)
from tests.support import RecordingStatusDisplay

_AUTO_FILE_ISSUE = "pycastle.iteration.hard_agent_error_report.auto_file_issue"


def _make_err(
    message: str = "",
    service_name: str = "claude",
    caller: str = "Implementer",
    status_code: int | None = None,
) -> HardAgentError:
    err = HardAgentError(message=message, service_name=service_name)
    err.caller = caller
    if status_code is not None:
        setattr(err, "status_code", status_code)  # noqa: B010
    return err


def _printed(display: RecordingStatusDisplay) -> list[str]:
    return [msg for op, *rest in display.calls if op == "print" for msg in [rest[1]]]


# ── Printed status message format ─────────────────────────────────────────────


def test_printed_status_includes_status_code():
    raw = json.dumps({"result": "oops", "status": 503})
    display = RecordingStatusDisplay()
    err = _make_err(message=raw, service_name="claude")

    with patch(_AUTO_FILE_ISSUE, return_value=None):
        translate_hard_agent_error_to_abort(err, Config(), display)

    msgs = _printed(display)
    assert any("hard API error: status 503" in m for m in msgs)


def test_printed_status_no_status_when_absent():
    display = RecordingStatusDisplay()
    err = _make_err(message="plain error", service_name="claude")

    with patch(_AUTO_FILE_ISSUE, return_value=None):
        translate_hard_agent_error_to_abort(err, Config(), display)

    msgs = _printed(display)
    assert any("hard API error: status no status" in m for m in msgs)


def test_url_suffix_appended_when_auto_file_issue_returns_url():
    url = "https://github.com/owner/repo/issues/99"
    display = RecordingStatusDisplay()
    err = _make_err(message="error", service_name="claude")

    with patch(_AUTO_FILE_ISSUE, return_value=url):
        translate_hard_agent_error_to_abort(err, Config(), display)

    msgs = _printed(display)
    assert any(f" — {url}" in m for m in msgs)


def test_no_url_suffix_when_auto_file_issue_returns_none():
    display = RecordingStatusDisplay()
    err = _make_err(message="error", service_name="claude")

    with patch(_AUTO_FILE_ISSUE, return_value=None):
        translate_hard_agent_error_to_abort(err, Config(), display)

    msgs = _printed(display)
    assert all(" — " not in m for m in msgs)


# ── Returned AbortedHardApiError.status_code ──────────────────────────────────


def test_status_code_from_json_envelope():
    raw = json.dumps({"result": "some error", "status": 429})
    display = RecordingStatusDisplay()
    err = _make_err(message=raw, service_name="claude")

    with patch(_AUTO_FILE_ISSUE, return_value=None):
        result = translate_hard_agent_error_to_abort(err, Config(), display)

    assert result == AbortedHardApiError(status_code=429)


def test_boolean_status_in_envelope_falls_back_to_caller_provided():
    raw = json.dumps({"result": "some error", "status": True})
    display = RecordingStatusDisplay()
    err = _make_err(message=raw, service_name="claude", status_code=500)

    with patch(_AUTO_FILE_ISSUE, return_value=None):
        result = translate_hard_agent_error_to_abort(err, Config(), display)

    assert result == AbortedHardApiError(status_code=500)


def test_none_status_in_envelope_with_no_fallback():
    raw = json.dumps({"result": "some error", "status": None})
    display = RecordingStatusDisplay()
    err = _make_err(message=raw, service_name="claude")

    with patch(_AUTO_FILE_ISSUE, return_value=None):
        result = translate_hard_agent_error_to_abort(err, Config(), display)

    assert result == AbortedHardApiError(status_code=None)


def test_missing_status_with_no_fallback_yields_none():
    raw = json.dumps({"result": "some error"})
    display = RecordingStatusDisplay()
    err = _make_err(message=raw, service_name="claude")

    with patch(_AUTO_FILE_ISSUE, return_value=None):
        result = translate_hard_agent_error_to_abort(err, Config(), display)

    assert result == AbortedHardApiError(status_code=None)


# ── Never-raise invariant ─────────────────────────────────────────────────────


def test_never_raises_when_auto_file_issue_returns_none():
    display = RecordingStatusDisplay()
    err = _make_err(message="error")

    with patch(_AUTO_FILE_ISSUE, return_value=None):
        result = translate_hard_agent_error_to_abort(err, Config(), display)

    assert isinstance(result, AbortedHardApiError)


# ── auto_file_issue called with correct arguments ─────────────────────────────


def test_auto_file_issue_called_with_composed_arguments():
    display = RecordingStatusDisplay()
    err = _make_err(message="plain error", service_name="claude")

    with patch(_AUTO_FILE_ISSUE, return_value=None) as mock_filer:
        translate_hard_agent_error_to_abort(err, Config(), display)

    mock_filer.assert_called_once()
    title, _body, labels = mock_filer.call_args.args
    assert "[pycastle] Claude API" in title
    assert labels == ["bug", "needs-triage"]
