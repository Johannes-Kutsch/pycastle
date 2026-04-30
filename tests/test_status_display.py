from pathlib import Path


from pycastle.iteration._deps import (
    NullStatusDisplay,
    RecordingStatusDisplay,
    StatusDisplay,
)


# ── NullStatusDisplay protocol conformance ────────────────────────────────────


def test_null_status_display_satisfies_protocol() -> None:
    assert isinstance(NullStatusDisplay(), StatusDisplay)


def test_recording_status_display_satisfies_protocol() -> None:
    assert isinstance(RecordingStatusDisplay(), StatusDisplay)


# ── NullStatusDisplay behaviour ───────────────────────────────────────────────


def test_null_add_agent_does_not_raise() -> None:
    d = NullStatusDisplay()
    d.add_agent("implementer-1", "Setup", Path("/tmp/agent.log"))


def test_null_update_phase_does_not_raise() -> None:
    d = NullStatusDisplay()
    d.update_phase("implementer-1", "Work")


def test_null_remove_agent_does_not_raise() -> None:
    d = NullStatusDisplay()
    d.remove_agent("implementer-1")


def test_null_print_delegates_to_builtins(capsys) -> None:
    d = NullStatusDisplay()
    d.print("Planning complete. 3 issue(s)")
    captured = capsys.readouterr()
    assert captured.out == "Planning complete. 3 issue(s)\n"


# ── RecordingStatusDisplay behaviour ─────────────────────────────────────────


def test_recording_starts_empty() -> None:
    d = RecordingStatusDisplay()
    assert d.calls == []


def test_recording_captures_add_agent() -> None:
    d = RecordingStatusDisplay()
    log_path = Path("/tmp/agent.log")
    d.add_agent("implementer-1", "Setup", log_path)
    assert d.calls == [("add_agent", "implementer-1", "Setup", log_path)]


def test_recording_captures_update_phase() -> None:
    d = RecordingStatusDisplay()
    d.update_phase("implementer-1", "Work")
    assert d.calls == [("update_phase", "implementer-1", "Work")]


def test_recording_captures_remove_agent() -> None:
    d = RecordingStatusDisplay()
    d.remove_agent("implementer-1")
    assert d.calls == [("remove_agent", "implementer-1")]


def test_recording_captures_print() -> None:
    d = RecordingStatusDisplay()
    d.print("Planning complete.")
    assert d.calls == [("print", "Planning complete.")]


def test_recording_accumulates_multiple_calls() -> None:
    d = RecordingStatusDisplay()
    log_path = Path("/tmp/agent.log")

    d.add_agent("implementer-1", "Setup", log_path)
    d.update_phase("implementer-1", "Work")
    d.remove_agent("implementer-1")

    assert d.calls == [
        ("add_agent", "implementer-1", "Setup", log_path),
        ("update_phase", "implementer-1", "Work"),
        ("remove_agent", "implementer-1"),
    ]
