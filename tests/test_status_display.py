import re
from pathlib import Path

from rich.console import Console

from pycastle.iteration._deps import (
    NullStatusDisplay,
    RecordingStatusDisplay,
    StatusDisplay,
)
from pycastle.rich_status_display import RichStatusDisplay


# ── RichStatusDisplay protocol conformance ────────────────────────────────────


def test_rich_status_display_satisfies_protocol() -> None:
    assert isinstance(RichStatusDisplay(), StatusDisplay)


# ── RichStatusDisplay behaviour ───────────────────────────────────────────────


def test_rich_stop_when_no_agents_added_is_safe() -> None:
    d = RichStatusDisplay()
    d.stop()


def test_rich_stop_is_idempotent() -> None:
    d = RichStatusDisplay()
    d.add_agent("Planner", "Setup", Path("/tmp/planner.log"), "")
    d.remove_agent("Planner")
    d.stop()


def test_rich_update_phase_for_unknown_agent_is_safe() -> None:
    d = RichStatusDisplay()
    d.update_phase("never-added", "Work")


def test_rich_remove_unknown_agent_is_safe() -> None:
    d = RichStatusDisplay()
    d.remove_agent("never-added")


def test_rich_print_outputs_message(capsys) -> None:
    d = RichStatusDisplay()
    d.print("hello world")
    assert "hello world" in capsys.readouterr().out


def test_rich_agents_render_sorted_by_phase_rank() -> None:
    d = RichStatusDisplay()
    d.add_agent("Merger", "Work", Path("/tmp/merger.log"), "")
    d.add_agent("Reviewer #5", "Work", Path("/tmp/rev5.log"), "")
    d.add_agent("Implementer #5", "Work", Path("/tmp/impl5.log"), "")
    d.add_agent("Planner", "Plan", Path("/tmp/planner.log"), "")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert output.find("Planner") < output.find("Implementer #5")
    assert output.find("Implementer #5") < output.find("Reviewer #5")
    assert output.find("Reviewer #5") < output.find("Merger")
    d.stop()


def test_rich_implementers_render_sorted_by_issue_number() -> None:
    d = RichStatusDisplay()
    d.add_agent("Implementer #42", "Work", Path("/tmp/impl42.log"), "")
    d.add_agent("Implementer #7", "Work", Path("/tmp/impl7.log"), "")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert output.find("Implementer #7") < output.find("Implementer #42")
    d.stop()


def test_rich_unknown_agent_sorts_after_known_phases() -> None:
    d = RichStatusDisplay()
    d.add_agent("Planner", "Plan", Path("/tmp/planner.log"), "")
    d.add_agent("Unknown-agent", "Work", Path("/tmp/unknown.log"), "")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert output.find("Planner") < output.find("Unknown-agent")
    d.stop()


def test_rich_renders_issue_headline_in_output() -> None:
    d = RichStatusDisplay()
    d.add_agent("Planner", "Plan", Path("/tmp/planner.log"), "Fix the login bug")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert "Fix the login bug" in output
    d.stop()


def test_rich_renders_no_header_row() -> None:
    d = RichStatusDisplay()
    d.add_agent("Planner", "Plan", Path("/tmp/planner.log"), "Fix bug")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert "Idle (s)" not in output
    assert "Log" not in output
    d.stop()


def test_rich_renders_blank_line_before_agent_rows() -> None:
    d = RichStatusDisplay()
    d.add_agent("Planner", "Plan", Path("/tmp/planner.log"), "Fix bug")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    planner_pos = output.find("Planner")
    lines_before = output[:planner_pos].split("\n")
    assert any(line.strip() == "" for line in lines_before)
    d.stop()


def test_rich_elapsed_format_shows_seconds_under_one_minute() -> None:
    d = RichStatusDisplay()
    d.add_agent("Planner", "Plan", Path("/tmp/planner.log"), "Fix bug")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert re.search(r"\d+s", output)
    d.stop()


def test_rich_elapsed_format_shows_minutes_and_seconds(monkeypatch) -> None:
    import pycastle.rich_status_display as mod

    times = iter([0.0, 312.0, 42.0])
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(times))

    d = RichStatusDisplay()
    d.add_agent("Planner", "Plan", Path("/tmp/planner.log"), "Fix bug")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert "5m 12s" in output
    assert "42s" in output
    d.stop()


def test_rich_update_message_for_unknown_agent_is_safe() -> None:
    d = RichStatusDisplay()
    d.update_message("never-added", "some message")


# ── NullStatusDisplay protocol conformance ────────────────────────────────────


def test_null_status_display_satisfies_protocol() -> None:
    assert isinstance(NullStatusDisplay(), StatusDisplay)


def test_recording_status_display_satisfies_protocol() -> None:
    assert isinstance(RecordingStatusDisplay(), StatusDisplay)


# ── NullStatusDisplay behaviour ───────────────────────────────────────────────


def test_null_add_agent_is_silent(capsys) -> None:
    d = NullStatusDisplay()
    d.add_agent("implementer-1", "Setup", Path("/tmp/agent.log"), "Fix login bug")
    assert capsys.readouterr().out == ""


def test_null_update_phase_is_silent(capsys) -> None:
    d = NullStatusDisplay()
    d.update_phase("implementer-1", "Work")
    assert capsys.readouterr().out == ""


def test_null_remove_agent_is_silent(capsys) -> None:
    d = NullStatusDisplay()
    d.remove_agent("implementer-1")
    assert capsys.readouterr().out == ""


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
    d.add_agent("implementer-1", "Setup", log_path, "Fix login bug")
    assert d.calls == [
        ("add_agent", "implementer-1", "Setup", log_path, "Fix login bug")
    ]


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


def test_recording_print_produces_no_stdout(capsys) -> None:
    d = RecordingStatusDisplay()
    d.print("Planning complete.")
    assert capsys.readouterr().out == ""


def test_recording_accumulates_multiple_calls() -> None:
    d = RecordingStatusDisplay()
    log_path = Path("/tmp/agent.log")

    d.add_agent("implementer-1", "Setup", log_path, "Fix login bug")
    d.update_phase("implementer-1", "Work")
    d.remove_agent("implementer-1")

    assert d.calls == [
        ("add_agent", "implementer-1", "Setup", log_path, "Fix login bug"),
        ("update_phase", "implementer-1", "Work"),
        ("remove_agent", "implementer-1"),
    ]
