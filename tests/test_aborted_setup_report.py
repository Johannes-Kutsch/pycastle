"""Interface-level tests for iteration.aborted_setup_report.

Tests verify that translate_aborted_setup_to_directive produces titles, bodies, and
printed status messages byte-for-byte identical to the inline AbortedSetup handling
in route_outcome, for every relevant AbortedSetup shape.
"""

from __future__ import annotations

from pycastle.config import Config
from pycastle.iteration import AbortedSetup
from pycastle.iteration.aborted_setup_report import translate_aborted_setup_to_directive
from pycastle.iteration.outcome_routing import ExitFailure
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


def _printed(display: RecordingStatusDisplay) -> list[str]:
    return [msg for op, *rest in display.calls if op == "print" for msg in [rest[1]]]


# ── Phase and message only ────────────────────────────────────────────────────


def test_phase_message_only_title():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(phase="git", message="repository not found")

    translate_aborted_setup_to_directive(outcome, Config(), display, filer)

    title, _, _ = filer.calls[0]
    assert title == "[pycastle] git setup failure: repository not found"


def test_phase_message_only_body():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(phase="git", message="repository not found")

    translate_aborted_setup_to_directive(outcome, Config(), display, filer)

    _, body, _ = filer.calls[0]
    assert body == (
        "## Setup phase failure\n\nPhase: git\n\n```\nrepository not found\n```\n"
    )


def test_phase_message_only_body_no_command_section():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(phase="git", message="repository not found")

    translate_aborted_setup_to_directive(outcome, Config(), display, filer)

    _, body, _ = filer.calls[0]
    assert "Command:" not in body


def test_phase_message_only_body_no_output_section():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(phase="git", message="repository not found")

    translate_aborted_setup_to_directive(outcome, Config(), display, filer)

    _, body, _ = filer.calls[0]
    assert "Output:" not in body


def test_phase_message_only_printed_status():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(phase="git", message="repository not found")

    translate_aborted_setup_to_directive(outcome, Config(), display, filer)

    msgs = _printed(display)
    assert any("git setup failed: repository not found" in m for m in msgs)


def test_phase_message_only_printed_status_no_command_line():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(phase="git", message="repository not found")

    translate_aborted_setup_to_directive(outcome, Config(), display, filer)

    msgs = _printed(display)
    assert all("Command:" not in m for m in msgs)


def test_phase_message_only_printed_status_no_output_line():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(phase="git", message="repository not found")

    translate_aborted_setup_to_directive(outcome, Config(), display, filer)

    msgs = _printed(display)
    assert all("Output:" not in m for m in msgs)


def test_phase_message_only_returns_exit_failure():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(phase="git", message="repository not found")

    result = translate_aborted_setup_to_directive(outcome, Config(), display, filer)

    assert result == ExitFailure(code=1)


# ── With command ──────────────────────────────────────────────────────────────


def test_with_command_body_includes_command():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(
        phase="clone",
        message="exit code 128",
        command="git clone https://example.com/repo.git",
    )

    translate_aborted_setup_to_directive(outcome, Config(), display, filer)

    _, body, _ = filer.calls[0]
    assert "Command: `git clone https://example.com/repo.git`" in body


def test_with_command_printed_status_includes_command():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(
        phase="clone",
        message="exit code 128",
        command="git clone https://example.com/repo.git",
    )

    translate_aborted_setup_to_directive(outcome, Config(), display, filer)

    msgs = _printed(display)
    assert any("Command: git clone https://example.com/repo.git" in m for m in msgs)


def test_with_command_body_no_output_section():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(
        phase="clone",
        message="exit code 128",
        command="git clone https://example.com/repo.git",
    )

    translate_aborted_setup_to_directive(outcome, Config(), display, filer)

    _, body, _ = filer.calls[0]
    assert "Output:" not in body


# ── With output ───────────────────────────────────────────────────────────────


def test_with_output_body_includes_output_block():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(
        phase="install", message="pip failed", output="error: package not found"
    )

    translate_aborted_setup_to_directive(outcome, Config(), display, filer)

    _, body, _ = filer.calls[0]
    assert "Output:\n\n```\nerror: package not found\n```\n" in body


def test_with_output_printed_status_includes_output():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(
        phase="install", message="pip failed", output="error: package not found"
    )

    translate_aborted_setup_to_directive(outcome, Config(), display, filer)

    msgs = _printed(display)
    assert any("Output: error: package not found" in m for m in msgs)


def test_with_output_body_no_command_section():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(
        phase="install", message="pip failed", output="error: package not found"
    )

    translate_aborted_setup_to_directive(outcome, Config(), display, filer)

    _, body, _ = filer.calls[0]
    assert "Command:" not in body


# ── With both command and output ──────────────────────────────────────────────


def test_with_command_and_output_body_includes_both():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(
        phase="build",
        message="make failed",
        command="make all",
        output="undefined reference to main",
    )

    translate_aborted_setup_to_directive(outcome, Config(), display, filer)

    _, body, _ = filer.calls[0]
    assert "Command: `make all`" in body
    assert "Output:\n\n```\nundefined reference to main\n```\n" in body


def test_with_command_and_output_printed_status_includes_both():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(
        phase="build",
        message="make failed",
        command="make all",
        output="undefined reference to main",
    )

    translate_aborted_setup_to_directive(outcome, Config(), display, filer)

    msgs = _printed(display)
    assert any(
        "Command: make all" in m and "Output: undefined reference to main" in m
        for m in msgs
    )


def test_with_command_and_output_returns_exit_failure():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(
        phase="build",
        message="make failed",
        command="make all",
        output="undefined reference to main",
    )

    result = translate_aborted_setup_to_directive(outcome, Config(), display, filer)

    assert result == ExitFailure(code=1)


# ── Bug-report labels ─────────────────────────────────────────────────────────


def test_bug_filer_called_with_bug_report_labels():
    from pycastle.bug_reporter import BUG_REPORT_LABEL_LIST

    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(phase="git", message="failed")

    translate_aborted_setup_to_directive(outcome, Config(), display, filer)

    _, _, labels = filer.calls[0]
    assert labels == BUG_REPORT_LABEL_LIST


# ── URL suffix in printed status ──────────────────────────────────────────────


def test_filer_returns_url_suffix_appended():
    url = "https://github.com/owner/repo/issues/42"
    filer = RecordingBugFiler(return_url=url)
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(phase="git", message="repository not found")

    translate_aborted_setup_to_directive(outcome, Config(), display, filer)

    msgs = _printed(display)
    assert any(f"\nReport: {url}" in m for m in msgs)


def test_filer_returns_none_no_report_suffix():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(phase="git", message="repository not found")

    translate_aborted_setup_to_directive(outcome, Config(), display, filer)

    msgs = _printed(display)
    assert all("Report:" not in m for m in msgs)


def test_filer_returns_empty_string_no_report_suffix():
    filer = RecordingBugFiler(return_url="")
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(phase="git", message="repository not found")

    translate_aborted_setup_to_directive(outcome, Config(), display, filer)

    msgs = _printed(display)
    assert all("Report:" not in m for m in msgs)


# ── Printed status uses empty caller ──────────────────────────────────────────


def test_printed_status_uses_empty_caller():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(phase="git", message="repository not found")

    translate_aborted_setup_to_directive(outcome, Config(), display, filer)

    callers = [caller for op, caller, *_ in display.calls if op == "print"]
    assert all(caller == "" for caller in callers)


# ── Title uses first line of multiline message ────────────────────────────────


def test_title_uses_first_line_of_multiline_message():
    filer = RecordingBugFiler(return_url=None)
    display = RecordingStatusDisplay()
    outcome = AbortedSetup(phase="git", message="first line\nsecond line\nthird line")

    translate_aborted_setup_to_directive(outcome, Config(), display, filer)

    title, _, _ = filer.calls[0]
    assert title == "[pycastle] git setup failure: first line"
