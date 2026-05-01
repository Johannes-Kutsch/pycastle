import io
import re

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
    d.add_agent("Planner", "Setup")
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
    d.add_agent("Merger", "Work")
    d.add_agent("Reviewer #5", "Work")
    d.add_agent("Implementer #5", "Work")
    d.add_agent("Planner", "Plan")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert output.find("Planner") < output.find("Implementer #5")
    assert output.find("Implementer #5") < output.find("Reviewer #5")
    assert output.find("Reviewer #5") < output.find("Merger")
    d.stop()


def test_rich_implementers_render_sorted_by_issue_number() -> None:
    d = RichStatusDisplay()
    d.add_agent("Implementer #42", "Work")
    d.add_agent("Implementer #7", "Work")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert output.find("Implementer #7") < output.find("Implementer #42")
    d.stop()


def test_rich_unknown_agent_sorts_after_known_phases() -> None:
    d = RichStatusDisplay()
    d.add_agent("Planner", "Plan")
    d.add_agent("Unknown-agent", "Work")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert output.find("Planner") < output.find("Unknown-agent")
    d.stop()


def test_rich_renders_agent_name_without_dash_suffix() -> None:
    d = RichStatusDisplay()
    d.add_agent("Implementer #42", "Work")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert "Implementer #42" in output
    assert " - " not in output
    d.stop()


def test_rich_renders_no_header_row() -> None:
    d = RichStatusDisplay()
    d.add_agent("Planner", "Plan")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert "Idle (s)" not in output
    assert "Log" not in output
    d.stop()


def test_rich_renders_blank_line_before_agent_rows() -> None:
    d = RichStatusDisplay()
    d.add_agent("Planner", "Plan")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    planner_pos = output.find("Planner")
    lines_before = output[:planner_pos].split("\n")
    assert any(line.strip() == "" for line in lines_before)
    d.stop()


def test_rich_elapsed_format_shows_seconds_under_one_minute() -> None:
    d = RichStatusDisplay()
    d.add_agent("Planner", "Plan")

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
    d.add_agent("Planner", "Plan")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert "5m 12s" in output
    assert "42s" in output
    d.stop()


def test_rich_phase_appears_in_output() -> None:
    d = RichStatusDisplay()
    d.add_agent("Planner", "DESIGNING")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert "DESIGNING" in output
    d.stop()


def test_rich_agent_names_are_right_aligned_by_elapsed_column(monkeypatch) -> None:
    import pycastle.rich_status_display as mod

    # #7 started at t=0, #42 started at t=270; render at t=312
    # elapsed #7 = 5m 12s (6 chars), elapsed #42 = 42s (3 chars)
    # right-justified elapsed column must be 6 chars wide for both rows,
    # so both "Implementer" names start at the same column.
    times = iter([0.0, 270.0] + [312.0] * 20)
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(times))

    d = RichStatusDisplay()
    d.add_agent("Implementer #7", "Work")
    d.add_agent("Implementer #42", "Work")

    console = Console(record=True, width=200)
    console.print(d)
    lines = [ln for ln in console.export_text().splitlines() if "Implementer" in ln]
    assert len(lines) == 2
    impl7_line = next(ln for ln in lines if "#7" in ln)
    impl42_line = next(ln for ln in lines if "#42" in ln)
    assert impl7_line.index("Implementer") == impl42_line.index("Implementer")
    d.stop()


def test_rich_elapsed_format_at_exactly_one_minute(monkeypatch) -> None:
    import pycastle.rich_status_display as mod

    times = iter([0.0, 60.0, 60.0])
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(times))

    d = RichStatusDisplay()
    d.add_agent("Planner", "Plan")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert "1m 0s" in output
    d.stop()


def test_rich_reset_idle_timer_resets_idle_time(monkeypatch) -> None:
    import pycastle.rich_status_display as mod

    # started_at=0, reset at t=100, render at t=150
    # idle = 150-100 = 50s  (not 150-0=150s)
    times = iter([0.0, 100.0, 150.0, 150.0])
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(times))

    d = RichStatusDisplay()
    d.add_agent("Planner", "Plan")
    d.reset_idle_timer("Planner")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert "50s" in output
    d.stop()


def test_rich_reset_idle_timer_for_unknown_agent_is_safe() -> None:
    d = RichStatusDisplay()
    d.reset_idle_timer("never-added")


def test_rich_reviewers_render_sorted_by_issue_number() -> None:
    d = RichStatusDisplay()
    d.add_agent("Reviewer #42", "Review")
    d.add_agent("Reviewer #7", "Review")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert output.find("Reviewer #7") < output.find("Reviewer #42")
    d.stop()


def test_rich_add_agent_twice_with_same_name_is_safe() -> None:
    d = RichStatusDisplay()
    d.add_agent("Planner", "Setup")
    d.add_agent("Planner", "Plan")

    console = Console(record=True, width=200)
    console.print(d)
    lines = [ln for ln in console.export_text().splitlines() if "Planner" in ln]
    assert len(lines) == 1
    d.stop()


def test_rich_agent_name_renders_without_hyperlink() -> None:
    d = RichStatusDisplay()
    d.add_agent("Implementer #5", "Work")

    ansi = _ansi_output(d)
    d.stop()

    assert "\x1b]8" not in ansi


# ── Color scheme tests ────────────────────────────────────────────────────────


def _ansi_output(display: RichStatusDisplay) -> str:
    buf = io.StringIO()
    console = Console(file=buf, width=200, force_terminal=True, color_system="256")
    console.print(display)
    return buf.getvalue()


def _has_code(ansi: str, code: int) -> bool:
    """Return True if ANSI SGR code N appears in any escape sequence."""
    return bool(re.search(rf"\x1b\[(?:\d+;)*{code}(?:;\d+)*m", ansi))


def test_rich_phase_renders_blue_for_plan_agent() -> None:
    d = RichStatusDisplay()
    d.add_agent("Planner", "Plan")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 34)  # blue


def test_rich_phase_renders_orange1_for_implement_agent() -> None:
    d = RichStatusDisplay()
    d.add_agent("Implementer #5", "Work")

    ansi = _ansi_output(d)
    d.stop()

    assert "\x1b[38;5;214m" in ansi  # orange1 (256-color index)


def test_rich_phase_renders_yellow_for_review_agent() -> None:
    d = RichStatusDisplay()
    d.add_agent("Reviewer #5", "Review")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 33)  # yellow


def test_rich_phase_renders_green_for_merge_agent() -> None:
    d = RichStatusDisplay()
    d.add_agent("Merger", "Merge")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 32)  # green


def test_rich_phase_renders_green_for_merge_row() -> None:
    d = RichStatusDisplay()
    d.add_agent("merge", "Merging")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 32)  # green


def test_rich_agent_name_renders_bold() -> None:
    d = RichStatusDisplay()
    d.add_agent("Planner", "Plan")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 1)  # bold


def test_rich_digit_sequences_in_agent_name_render_cyan() -> None:
    d = RichStatusDisplay()
    d.add_agent("Implementer #5", "Work")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 36)  # cyan


def test_rich_non_numeric_agent_name_renders_bold_without_cyan() -> None:
    d = RichStatusDisplay()
    d.add_agent("Planner", "Plan")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 1)  # bold
    assert not _has_code(ansi, 36)  # no cyan


def test_rich_elapsed_column_renders_dim() -> None:
    d = RichStatusDisplay()
    d.add_agent("Planner", "Plan")

    ansi = _ansi_output(d)
    d.stop()

    # Elapsed column precedes the agent name — dim must appear before "Planner"
    before_name = ansi[: ansi.index("Planner")]
    assert _has_code(before_name, 2)  # dim


def test_rich_idle_column_renders_dim() -> None:
    d = RichStatusDisplay()
    d.add_agent("Planner", "Plan")

    ansi = _ansi_output(d)
    d.stop()

    # Idle column follows the agent name — dim must appear after "Planner"
    after_name = ansi[ansi.index("Planner") + len("Planner") :]
    assert _has_code(after_name, 2)  # dim


def test_rich_phase_renders_without_color_for_unknown_agent() -> None:
    d = RichStatusDisplay()
    d.add_agent("Unknown-agent", "Custom")

    ansi = _ansi_output(d)
    d.stop()

    assert not _has_code(ansi, 34)  # no blue
    assert "\x1b[38;5;214m" not in ansi  # no orange1
    assert not _has_code(ansi, 33)  # no yellow
    assert not _has_code(ansi, 32)  # no green


def test_rich_phase_renders_white_for_startup_agent() -> None:
    d = RichStatusDisplay()
    d.add_agent("startup", "Git identity")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 37)  # white


def test_rich_phase_renders_blue_for_preflight_checks_agent() -> None:
    d = RichStatusDisplay()
    d.add_agent("preflight-checks", "Setup")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 34)  # blue (same as plan stage)


# ── NullStatusDisplay protocol conformance ────────────────────────────────────


def test_null_status_display_satisfies_protocol() -> None:
    assert isinstance(NullStatusDisplay(), StatusDisplay)


def test_recording_status_display_satisfies_protocol() -> None:
    assert isinstance(RecordingStatusDisplay(), StatusDisplay)


# ── NullStatusDisplay behaviour ───────────────────────────────────────────────


def test_null_reset_idle_timer_is_silent(capsys) -> None:
    d = NullStatusDisplay()
    d.reset_idle_timer("implementer-1")
    assert capsys.readouterr().out == ""


def test_null_add_agent_is_silent(capsys) -> None:
    d = NullStatusDisplay()
    d.add_agent("implementer-1", "Setup")
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
    d.add_agent("implementer-1", "Setup")
    assert d.calls == [("add_agent", "implementer-1", "Setup")]


def test_recording_captures_update_phase() -> None:
    d = RecordingStatusDisplay()
    d.update_phase("implementer-1", "Work")
    assert d.calls == [("update_phase", "implementer-1", "Work")]


def test_recording_captures_remove_agent() -> None:
    d = RecordingStatusDisplay()
    d.remove_agent("implementer-1")
    assert d.calls == [("remove_agent", "implementer-1")]


def test_recording_captures_reset_idle_timer() -> None:
    d = RecordingStatusDisplay()
    d.reset_idle_timer("implementer-1")
    assert d.calls == [("reset_idle_timer", "implementer-1")]


def test_recording_accumulates_reset_idle_timer_calls() -> None:
    d = RecordingStatusDisplay()
    d.reset_idle_timer("implementer-1")
    d.reset_idle_timer("implementer-1")
    assert d.calls == [
        ("reset_idle_timer", "implementer-1"),
        ("reset_idle_timer", "implementer-1"),
    ]


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

    d.add_agent("implementer-1", "Setup")
    d.update_phase("implementer-1", "Work")
    d.remove_agent("implementer-1")

    assert d.calls == [
        ("add_agent", "implementer-1", "Setup"),
        ("update_phase", "implementer-1", "Work"),
        ("remove_agent", "implementer-1"),
    ]
