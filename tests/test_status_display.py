import io
import re

from rich.console import Console

from pycastle.iteration._deps import RecordingStatusDisplay
from pycastle.status_display import PlainStatusDisplay, StatusDisplay
from pycastle.rich_status_display import RichStatusDisplay


# ── RichStatusDisplay behaviour ───────────────────────────────────────────────


def test_rich_stop_when_no_agents_added_is_safe() -> None:
    d = RichStatusDisplay()
    d.stop()


def test_rich_stop_is_idempotent() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent")
    d.remove("Plan Agent")
    d.stop()


def test_rich_update_phase_for_unknown_agent_is_safe() -> None:
    d = RichStatusDisplay()
    d.update_phase("never-added", "Work")


def test_rich_remove_unknown_agent_is_safe() -> None:
    d = RichStatusDisplay()
    d.remove("never-added")


def test_rich_print_outputs_message(capsys) -> None:
    d = RichStatusDisplay()
    d.print("caller", "hello world")
    assert "hello world" in capsys.readouterr().out


def test_rich_print_blank_line_on_first_print(capsys) -> None:
    d = RichStatusDisplay()
    d.print("test", "hello")
    out = capsys.readouterr().out
    assert out.startswith("\n") and "[test] hello" in out


def test_rich_print_no_blank_line_on_same_caller_repeat(capsys) -> None:
    d = RichStatusDisplay()
    d.print("block", "hello")
    d.print("block", "world")
    out = capsys.readouterr().out
    assert "[block] hello\n[block] world" in out


def test_rich_print_blank_line_on_caller_change(capsys) -> None:
    d = RichStatusDisplay()
    d.print("block-a", "hello")
    d.print("block-b", "world")
    out = capsys.readouterr().out
    assert "hello\n\n" in out and "world" in out


def test_rich_print_blank_line_when_switching_from_empty_to_named_caller(
    capsys,
) -> None:
    d = RichStatusDisplay()
    d.print("", "hello")
    d.print("block-a", "world")
    out = capsys.readouterr().out
    assert "hello\n\n" in out and "world" in out


def test_rich_print_blank_line_when_switching_from_named_to_empty_caller(
    capsys,
) -> None:
    d = RichStatusDisplay()
    d.print("block-a", "hello")
    d.print("", "world")
    out = capsys.readouterr().out
    assert "hello\n\n" in out and "world" in out


def test_rich_agents_render_sorted_by_phase_rank() -> None:
    d = RichStatusDisplay()
    d.register("Merge Agent")
    d.register("Review Agent #5")
    d.register("Implement Agent #5")
    d.register("Plan Agent")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert output.find("Plan Agent") < output.find("Implement Agent #5")
    assert output.find("Implement Agent #5") < output.find("Review Agent #5")
    assert output.find("Review Agent #5") < output.find("Merge Agent")
    d.stop()


def test_rich_implementers_render_sorted_by_issue_number() -> None:
    d = RichStatusDisplay()
    d.register("Implement Agent #42")
    d.register("Implement Agent #7")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert output.find("Implement Agent #7") < output.find("Implement Agent #42")
    d.stop()


def test_rich_unknown_agent_sorts_after_known_phases() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent")
    d.register("Unknown-agent")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert output.find("Plan Agent") < output.find("Unknown-agent")
    d.stop()


def test_rich_renders_agent_name_without_dash_suffix() -> None:
    d = RichStatusDisplay()
    d.register("Implement Agent #42")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert "Implement Agent #42" in output
    assert " - " not in output
    d.stop()


def test_rich_renders_no_header_row() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert "Idle (s)" not in output
    assert "Log" not in output
    d.stop()


def test_rich_renders_blank_line_before_agent_rows() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    planner_pos = output.find("Plan Agent")
    lines_before = output[:planner_pos].split("\n")
    assert any(line.strip() == "" for line in lines_before)
    d.stop()


def test_rich_elapsed_format_shows_seconds_under_one_minute() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent")

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
    d.register("Plan Agent")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert "5m 12s" in output
    assert "42s" in output
    d.stop()


def test_rich_phase_appears_in_output() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent")
    d.update_phase("Plan Agent", "DESIGNING")

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
    # so both "Implement Agent" names start at the same column.
    times = iter([0.0, 270.0] + [312.0] * 20)
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(times))

    d = RichStatusDisplay()
    d.register("Implement Agent #7")
    d.register("Implement Agent #42")

    console = Console(record=True, width=200)
    console.print(d)
    lines = [ln for ln in console.export_text().splitlines() if "Implement Agent" in ln]
    assert len(lines) == 2
    impl7_line = next(ln for ln in lines if "#7" in ln)
    impl42_line = next(ln for ln in lines if "#42" in ln)
    assert impl7_line.index("Implement Agent") == impl42_line.index("Implement Agent")
    d.stop()


def test_rich_elapsed_format_at_exactly_one_minute(monkeypatch) -> None:
    import pycastle.rich_status_display as mod

    times = iter([0.0, 60.0, 60.0])
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(times))

    d = RichStatusDisplay()
    d.register("Plan Agent")

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
    d.register("Plan Agent")
    d.reset_idle_timer("Plan Agent")

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
    d.register("Review Agent #42")
    d.register("Review Agent #7")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert output.find("Review Agent #7") < output.find("Review Agent #42")
    d.stop()


def test_rich_register_twice_with_same_name_is_safe() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent")
    d.register("Plan Agent")

    console = Console(record=True, width=200)
    console.print(d)
    lines = [ln for ln in console.export_text().splitlines() if "Plan Agent" in ln]
    assert len(lines) == 1
    d.stop()


def test_rich_agent_name_renders_without_hyperlink() -> None:
    d = RichStatusDisplay()
    d.register("Implement Agent #5")

    ansi = _ansi_output(d)
    d.stop()

    assert "\x1b]8" not in ansi


# ── Body column tests ────────────────────────────────────────────────────────


def test_rich_body_shows_lifecycle_phase_during_non_work() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", work_body="Creating Plan from 3 issues")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()
    d.stop()

    assert "Setup" in output
    assert "Creating Plan from 3 issues" not in output


def test_rich_body_shows_work_body_during_work() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", work_body="Creating Plan from 3 issues")
    d.update_phase("Plan Agent", "Work")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()
    d.stop()

    assert "Creating Plan from 3 issues" in output
    assert "Work" not in output


def test_rich_body_is_unstyled() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent")

    buf = io.StringIO()
    console = Console(file=buf, width=200, force_terminal=True, color_system="256")
    console.print(d)
    ansi = buf.getvalue()
    d.stop()

    body_idx = ansi.index("Setup")
    before_body = ansi[max(0, body_idx - 30) : body_idx]
    assert not re.search(r"\x1b\[(?:\d+;)*(?:3[0-9]|9[0-7])(?:;\d+)*m", before_body)


def test_rich_body_is_blank_when_work_body_is_empty() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent")
    d.update_phase("Plan Agent", "Work")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()
    d.stop()

    assert "Plan Agent" in output
    assert "Work" not in output


def test_rich_body_shows_work_body_after_phase_transitions_to_work() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", work_body="Creating Plan from 3 issues")
    d.update_phase("Plan Agent", "Work")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()
    d.stop()

    assert "Creating Plan from 3 issues" in output
    assert "Setup" not in output


def test_rich_body_reverts_to_phase_name_after_transitioning_from_work() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", work_body="Creating Plan from 3 issues")
    d.update_phase("Plan Agent", "Work")
    d.update_phase("Plan Agent", "Prepare")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()
    d.stop()

    assert "Prepare" in output
    assert "Creating Plan from 3 issues" not in output


def test_rich_register_with_initial_phase_shows_that_phase() -> None:
    d = RichStatusDisplay()
    d.register("Preflight", initial_phase="Running")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()
    d.stop()

    assert "Running" in output
    assert "Setup" not in output


def test_rich_pre_flight_agent_sorts_before_implementers() -> None:
    d = RichStatusDisplay()
    d.register("Implement Agent #1")
    d.register("Preflight Agent")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()
    d.stop()

    assert output.find("Preflight Agent") < output.find("Implement Agent")


# ── Color scheme tests ────────────────────────────────────────────────────────


def _ansi_output(display: RichStatusDisplay) -> str:
    buf = io.StringIO()
    console = Console(file=buf, width=200, force_terminal=True, color_system="256")
    console.print(display)
    return buf.getvalue()


def _has_code(ansi: str, code: int) -> bool:
    """Return True if ANSI SGR code N appears in any escape sequence."""
    return bool(re.search(rf"\x1b\[(?:\d+;)*{code}(?:;\d+)*m", ansi))


def test_rich_agent_name_renders_bold() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 1)  # bold


def test_rich_digit_sequences_in_agent_name_render_cyan() -> None:
    d = RichStatusDisplay()
    d.register("Implement Agent #5")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 36)  # cyan


def test_rich_non_numeric_agent_name_renders_bold_without_cyan() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 1)  # bold
    assert not _has_code(ansi, 36)  # no cyan


def test_rich_elapsed_column_renders_dim() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent")

    ansi = _ansi_output(d)
    d.stop()

    # Elapsed column precedes the agent name — dim must appear before "Plan Agent"
    before_name = ansi[: ansi.index("Plan Agent")]
    assert _has_code(before_name, 2)  # dim


def test_rich_idle_column_renders_dim() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent")

    ansi = _ansi_output(d)
    d.stop()

    # Idle column follows the agent name — dim must appear after "Plan Agent"
    after_name = ansi[ansi.index("Plan Agent") + len("Plan Agent") :]
    assert _has_code(after_name, 2)  # dim


def test_rich_role_name_renders_without_color_for_unknown_agent() -> None:
    d = RichStatusDisplay()
    d.register("Unknown-agent")

    ansi = _ansi_output(d)
    d.stop()

    assert not _has_code(ansi, 34)  # no blue
    assert not _has_code(ansi, 214)  # no orange1
    assert not _has_code(ansi, 33)  # no yellow
    assert not _has_code(ansi, 32)  # no green


def test_rich_planner_name_renders_bold_without_role_color() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 1)  # bold
    assert not _has_code(ansi, 34)  # no blue


def test_rich_implementer_name_renders_bold_without_role_color() -> None:
    d = RichStatusDisplay()
    d.register("Implement Agent #5")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 1)  # bold
    assert not _has_code(ansi, 214)  # no orange1


def test_rich_reviewer_name_renders_bold_without_role_color() -> None:
    d = RichStatusDisplay()
    d.register("Review Agent #3")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 1)  # bold
    assert not _has_code(ansi, 33)  # no yellow


def test_rich_merger_name_renders_bold_without_role_color() -> None:
    d = RichStatusDisplay()
    d.register("Merge Agent")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 1)  # bold
    assert not _has_code(ansi, 32)  # no green


def test_rich_pre_flight_name_renders_bold_without_role_color() -> None:
    d = RichStatusDisplay()
    d.register("Preflight Agent")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 1)  # bold
    assert not _has_code(ansi, 129)  # no purple


def test_rich_pre_flight_reporter_name_renders_bold_without_role_color() -> None:
    d = RichStatusDisplay()
    d.register("Pre-Flight Reporter")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 1)  # bold
    assert not _has_code(ansi, 31)  # no red


# ── Protocol conformance ─────────────────────────────────────────────────────


def test_rich_status_display_satisfies_protocol() -> None:
    assert isinstance(RichStatusDisplay(), StatusDisplay)


# ── RichStatusDisplay new caller-based API ────────────────────────────────────


def _make_ansi_console() -> tuple[io.StringIO, Console]:
    buf = io.StringIO()
    console = Console(file=buf, width=200, force_terminal=True, color_system="standard")
    return buf, console


def test_rich_new_api_print_outputs_bracketed_prefix(capsys) -> None:
    d = RichStatusDisplay()
    d.print("Plan", "msg")
    assert "[Plan] msg" in capsys.readouterr().out


def test_rich_new_api_print_empty_caller_outputs_message_verbatim(capsys) -> None:
    d = RichStatusDisplay()
    d.print("", "no prefix")
    out = capsys.readouterr().out
    assert "no prefix" in out
    assert "[" not in out


def test_rich_new_api_print_error_style_renders_entire_line_in_red() -> None:
    buf, console = _make_ansi_console()
    d = RichStatusDisplay(console=console)
    d.print("X", "msg", style="error")
    ansi = buf.getvalue()
    caller_idx = ansi.find("[X]")
    assert caller_idx >= 0 and "msg" in ansi
    assert _has_code(ansi[:caller_idx], 31)  # red precedes [X]


def test_rich_new_api_print_success_style_renders_entire_line_in_green() -> None:
    buf, console = _make_ansi_console()
    d = RichStatusDisplay(console=console)
    d.print("X", "msg", style="success")
    ansi = buf.getvalue()
    caller_idx = ansi.find("[X]")
    assert caller_idx >= 0 and "msg" in ansi
    assert _has_code(ansi[:caller_idx], 32)  # green precedes [X]


def test_rich_new_api_register_prints_started(capsys) -> None:
    d = RichStatusDisplay()
    d.register("X")
    d.stop()
    assert "[X] started" in capsys.readouterr().out


def test_rich_new_api_register_custom_startup_message(capsys) -> None:
    d = RichStatusDisplay()
    d.register("X", startup_message="booting")
    d.stop()
    assert "[X] booting" in capsys.readouterr().out


def test_rich_new_api_remove_prints_finished_in_green() -> None:
    buf, console = _make_ansi_console()
    d = RichStatusDisplay(console=console)
    d.remove("X")
    ansi = buf.getvalue()
    assert "[X]" in ansi and "finished" in ansi
    # Green (32) must appear before [X] — [X] and message are separate bold/green spans
    assert _has_code(ansi[: ansi.find("[X]")], 32)


def test_rich_new_api_remove_error_style_prints_in_red() -> None:
    buf, console = _make_ansi_console()
    d = RichStatusDisplay(console=console)
    d.remove("X", shutdown_message="failed", shutdown_style="error")
    ansi = buf.getvalue()
    assert "[X]" in ansi and "failed" in ansi
    # Red (31) must appear before [X]
    assert _has_code(ansi[: ansi.find("[X]")], 31)


def test_rich_new_api_blank_line_on_caller_change(capsys) -> None:
    d = RichStatusDisplay()
    d.print("A", "first")
    d.print("B", "second")
    out = capsys.readouterr().out
    assert "[A] first\n\n[B] second" in out


def test_rich_new_api_no_blank_line_on_same_caller(capsys) -> None:
    d = RichStatusDisplay()
    d.print("A", "first")
    d.print("A", "second")
    out = capsys.readouterr().out
    assert "[A] first\n[A] second" in out


def test_rich_new_canonical_agent_names_sort_correctly() -> None:
    d = RichStatusDisplay()
    d.register("Merge Agent")
    d.register("Review Agent #3")
    d.register("Implement Agent #1")
    d.register("Plan Agent")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()
    d.stop()

    assert output.find("Plan Agent") < output.find("Implement Agent #1")
    assert output.find("Implement Agent #1") < output.find("Review Agent #3")
    assert output.find("Review Agent #3") < output.find("Merge Agent")


def test_rich_preflight_agent_sorts_before_plan_agent() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent")
    d.register("Preflight Agent")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()
    d.stop()

    assert output.find("Preflight Agent") < output.find("Plan Agent")


def test_rich_new_api_first_print_has_leading_blank_line(capsys) -> None:
    d = RichStatusDisplay()
    d.print("A", "hello")
    out = capsys.readouterr().out
    assert out.startswith("\n") and "[A] hello" in out


def test_rich_register_with_empty_caller_prints_message_only(capsys) -> None:
    d = RichStatusDisplay()
    d.register("", startup_message="booting")
    d.stop()
    out = capsys.readouterr().out
    assert "booting" in out
    assert "[" not in out


def test_rich_remove_with_empty_caller_prints_message_only(capsys) -> None:
    d = RichStatusDisplay()
    d.remove("", shutdown_message="done")
    out = capsys.readouterr().out
    assert "done" in out
    assert "[" not in out


def test_rich_remove_unregistered_caller_is_safe(capsys) -> None:
    d = RichStatusDisplay()
    d.remove("never-registered")
    assert "[never-registered] finished" in capsys.readouterr().out


def test_rich_multiple_registers_use_single_live(capsys) -> None:
    d = RichStatusDisplay()
    d.register("A")
    d.register("B")
    d.stop()
    out = capsys.readouterr().out
    assert "[A] started" in out
    assert "[B] started" in out


def test_rich_register_inserts_blank_line_when_caller_changes(capsys) -> None:
    d = RichStatusDisplay()
    d.register("X")
    d.register("Y")
    d.stop()
    out = capsys.readouterr().out
    assert "[X] started\n\n[Y] started" in out


def test_rich_register_blank_line_before_first_output(capsys) -> None:
    d = RichStatusDisplay()
    d.register("X")
    d.stop()
    out = capsys.readouterr().out
    assert out.startswith("\n") and "[X] started" in out


def test_rich_remove_inserts_blank_line_when_caller_changes(capsys) -> None:
    d = RichStatusDisplay()
    d.remove("X")
    d.remove("Y")
    out = capsys.readouterr().out
    assert "[X] finished\n\n[Y] finished" in out


def test_rich_remove_blank_line_before_first_output(capsys) -> None:
    d = RichStatusDisplay()
    d.remove("X")
    out = capsys.readouterr().out
    assert out.startswith("\n") and "[X] finished" in out


def test_rich_cross_method_blank_line_register_then_print(capsys) -> None:
    d = RichStatusDisplay()
    d.register("X")
    d.print("Y", "msg")
    d.stop()
    out = capsys.readouterr().out
    assert "[X] started\n\n[Y] msg" in out


def test_rich_cross_method_blank_line_remove_then_print(capsys) -> None:
    d = RichStatusDisplay()
    d.remove("X")
    d.print("Y", "msg")
    out = capsys.readouterr().out
    assert "[X] finished\n\n[Y] msg" in out


def test_rich_cross_method_no_blank_register_then_remove_same_caller(capsys) -> None:
    d = RichStatusDisplay()
    d.register("X")
    d.remove("X")
    out = capsys.readouterr().out
    assert "[X] started\n[X] finished" in out


def test_rich_print_anonymous_caller_always_inserts_blank_line(capsys) -> None:
    d = RichStatusDisplay()
    d.print("", "first")
    d.print("", "second")
    out = capsys.readouterr().out
    assert "first\n\nsecond" in out


def test_rich_print_caller_prefix_is_bold() -> None:
    buf, console = _make_ansi_console()
    d = RichStatusDisplay(console=console)
    d.print("Plan", "some message")
    ansi = buf.getvalue()
    caller_idx = ansi.find("[Plan]")
    assert caller_idx >= 0
    before_caller = ansi[:caller_idx]
    assert _has_code(before_caller, 1)  # bold before [Plan]


def test_rich_register_caller_prefix_is_bold() -> None:
    buf, console = _make_ansi_console()
    d = RichStatusDisplay(console=console)
    d.register("Plan", startup_message="started")
    ansi = buf.getvalue()
    # Bold code (1) must appear in the ANSI escape immediately before [Plan].
    # The Live panel renders "Plan" without brackets, so this pattern is unique
    # to the startup message line.
    assert re.search(r"\x1b\[(?:\d+;)*1(?:;\d+)*m\[Plan\]", ansi)


def test_rich_remove_caller_prefix_is_bold() -> None:
    buf, console = _make_ansi_console()
    d = RichStatusDisplay(console=console)
    d.remove("Plan", shutdown_message="finished")
    ansi = buf.getvalue()
    # Bold code (1) must appear immediately before [Plan]; may be combined with
    # green (e.g. \x1b[1;32m) since shutdown styling is applied to the whole line.
    assert re.search(r"\x1b\[(?:\d+;)*1(?:;\d+)*m\[Plan\]", ansi)


def test_rich_remove_caller_prefix_is_bold_with_error_style() -> None:
    buf, console = _make_ansi_console()
    d = RichStatusDisplay(console=console)
    d.remove("Plan", shutdown_message="failed", shutdown_style="error")
    ansi = buf.getvalue()
    # Bold (1) must appear immediately before [Plan]; red styling is combined (e.g. \x1b[1;31m).
    assert re.search(r"\x1b\[(?:\d+;)*1(?:;\d+)*m\[Plan\]", ansi)
    # Red (31) must also appear before [Plan].
    assert _has_code(ansi[: ansi.find("[Plan]")], 31)


def test_rich_remove_warning_style_renders_in_yellow() -> None:
    buf, console = _make_ansi_console()
    d = RichStatusDisplay(console=console)
    d.remove("X", shutdown_message="warning msg", shutdown_style="warning")
    ansi = buf.getvalue()
    assert "[X]" in ansi and "warning msg" in ansi
    assert _has_code(ansi[: ansi.find("[X]")], 33)  # yellow precedes [X]


def test_rich_remove_multiline_warning_style_renders_all_lines_in_yellow() -> None:
    buf, console = _make_ansi_console()
    d = RichStatusDisplay(console=console)
    d.remove("X", shutdown_message="line1\nline2", shutdown_style="warning")
    ansi = buf.getvalue()
    first_x = ansi.find("[X]")
    second_x = ansi.find("[X]", first_x + 1)
    assert first_x >= 0 and second_x >= 0
    assert _has_code(ansi[:first_x], 33)  # yellow before first [X]
    assert _has_code(ansi[first_x:second_x], 33)  # yellow before second [X]


def test_rich_print_message_after_caller_prefix_is_not_bold() -> None:
    buf, console = _make_ansi_console()
    d = RichStatusDisplay(console=console)
    d.print("Plan", "some message")
    ansi = buf.getvalue()
    msg_idx = ansi.find("some message")
    assert msg_idx >= 0
    # Between "] " and "some message" bold must be reset
    between = ansi[ansi.find("[Plan]") + len("[Plan]") : msg_idx]
    assert _has_code(between, 22) or not _has_code(ansi[msg_idx - 10 : msg_idx], 1)


def test_rich_register_no_blank_line_on_same_caller(capsys) -> None:
    d = RichStatusDisplay()
    d.register("X")
    d.register("X")
    d.stop()
    out = capsys.readouterr().out
    assert "[X] started\n[X] started" in out


def test_rich_remove_no_blank_line_on_same_caller(capsys) -> None:
    d = RichStatusDisplay()
    d.remove("X")
    d.remove("X")
    out = capsys.readouterr().out
    assert "[X] finished\n[X] finished" in out


def test_rich_named_to_anonymous_print_inserts_blank(capsys) -> None:
    d = RichStatusDisplay()
    d.print("Alice", "hello")
    d.print("", "anon")
    out = capsys.readouterr().out
    assert "hello\n\nanon" in out


def test_rich_anonymous_to_named_print_inserts_blank(capsys) -> None:
    d = RichStatusDisplay()
    d.print("", "anon")
    d.print("Alice", "hello")
    out = capsys.readouterr().out
    assert "anon\n\n[Alice] hello" in out


def test_rich_first_anonymous_print_has_leading_blank(capsys) -> None:
    d = RichStatusDisplay()
    d.print("", "first anon")
    out = capsys.readouterr().out
    assert out.startswith("\n") and "first anon" in out


def test_rich_register_anonymous_after_named_inserts_blank(capsys) -> None:
    d = RichStatusDisplay()
    d.print("Alice", "hello")
    d.register("", "anon start")
    d.stop()
    out = capsys.readouterr().out
    assert "hello\n\nanon start" in out


def test_plain_status_display_satisfies_protocol() -> None:
    assert isinstance(PlainStatusDisplay(), StatusDisplay)


def test_recording_status_display_satisfies_protocol() -> None:
    assert isinstance(RecordingStatusDisplay(), StatusDisplay)


# ── PlainStatusDisplay behaviour ───────────────────────────────────────────────


def test_plain_update_phase_produces_no_output(capsys) -> None:
    d = PlainStatusDisplay()
    d.update_phase("implementer-1", "Work")
    assert capsys.readouterr().out == ""


def test_plain_reset_idle_timer_produces_no_output(capsys) -> None:
    d = PlainStatusDisplay()
    d.reset_idle_timer("implementer-1")
    assert capsys.readouterr().out == ""


def test_plain_print_with_caller_outputs_bracketed_prefix(capsys) -> None:
    d = PlainStatusDisplay()
    d.print("Plan", "Planning complete. 3 issue(s)")
    assert capsys.readouterr().out == "\n[Plan] Planning complete. 3 issue(s)\n"


def test_plain_print_with_empty_caller_outputs_message_verbatim(capsys) -> None:
    d = PlainStatusDisplay()
    d.print("", "no prefix here")
    assert capsys.readouterr().out == "\nno prefix here\n"


def test_plain_print_style_is_ignored(capsys) -> None:
    d = PlainStatusDisplay()
    d.print("X", "msg", style="error")
    assert capsys.readouterr().out == "\n[X] msg\n"


def test_plain_register_defaults_print_started(capsys) -> None:
    d = PlainStatusDisplay()
    d.register("X")
    assert capsys.readouterr().out == "\n[X] started\n"


def test_plain_register_with_custom_startup_message(capsys) -> None:
    d = PlainStatusDisplay()
    d.register("X", startup_message="custom")
    assert capsys.readouterr().out == "\n[X] custom\n"


def test_plain_remove_defaults_print_finished(capsys) -> None:
    d = PlainStatusDisplay()
    d.remove("X")
    assert capsys.readouterr().out == "\n[X] finished\n"


def test_plain_remove_with_custom_shutdown_message(capsys) -> None:
    d = PlainStatusDisplay()
    d.remove("X", shutdown_message="failed", shutdown_style="error")
    assert capsys.readouterr().out == "\n[X] failed\n"


def test_plain_consecutive_same_caller_no_blank_line(capsys) -> None:
    d = PlainStatusDisplay()
    d.print("X", "first")
    d.print("X", "second")
    out = capsys.readouterr().out
    assert out == "\n[X] first\n[X] second\n"


def test_plain_different_caller_inserts_blank_line(capsys) -> None:
    d = PlainStatusDisplay()
    d.print("X", "from X")
    d.print("Y", "from Y")
    out = capsys.readouterr().out
    assert out == "\n[X] from X\n\n[Y] from Y\n"


def test_plain_first_print_has_leading_blank_line(capsys) -> None:
    d = PlainStatusDisplay()
    d.print("X", "msg")
    out = capsys.readouterr().out
    assert out.startswith("\n") and "[X] msg" in out


def test_plain_print_accepts_non_string_message(capsys) -> None:
    d = PlainStatusDisplay()
    d.print("X", 42)
    assert capsys.readouterr().out == "\n[X] 42\n"


def test_plain_print_caller_switch_and_back_inserts_blank_lines(capsys) -> None:
    d = PlainStatusDisplay()
    d.print("X", "first")
    d.print("Y", "second")
    d.print("X", "third")
    out = capsys.readouterr().out
    assert out == "\n[X] first\n\n[Y] second\n\n[X] third\n"


def test_plain_register_with_empty_caller_prints_message_only(capsys) -> None:
    d = PlainStatusDisplay()
    d.register("", startup_message="booting")
    assert capsys.readouterr().out == "\nbooting\n"


def test_plain_remove_with_empty_caller_prints_message_only(capsys) -> None:
    d = PlainStatusDisplay()
    d.remove("", shutdown_message="done")
    assert capsys.readouterr().out == "\ndone\n"


def test_plain_register_inserts_blank_line_when_caller_changes(capsys) -> None:
    d = PlainStatusDisplay()
    d.register("X")
    d.register("Y")
    out = capsys.readouterr().out
    assert out == "\n[X] started\n\n[Y] started\n"


def test_plain_register_blank_line_before_first_output(capsys) -> None:
    d = PlainStatusDisplay()
    d.register("X")
    out = capsys.readouterr().out
    assert out.startswith("\n") and "[X] started" in out


def test_plain_register_no_blank_line_on_same_caller(capsys) -> None:
    d = PlainStatusDisplay()
    d.register("X")
    d.register("X")
    out = capsys.readouterr().out
    assert out == "\n[X] started\n[X] started\n"


def test_plain_remove_inserts_blank_line_when_caller_changes(capsys) -> None:
    d = PlainStatusDisplay()
    d.remove("X")
    d.remove("Y")
    out = capsys.readouterr().out
    assert out == "\n[X] finished\n\n[Y] finished\n"


def test_plain_remove_blank_line_before_first_output(capsys) -> None:
    d = PlainStatusDisplay()
    d.remove("X")
    out = capsys.readouterr().out
    assert out.startswith("\n") and "[X] finished" in out


def test_plain_print_anonymous_caller_always_inserts_blank_line(capsys) -> None:
    d = PlainStatusDisplay()
    d.print("", "first")
    d.print("", "second")
    out = capsys.readouterr().out
    assert out == "\nfirst\n\nsecond\n"


def test_plain_cross_method_blank_line_register_then_print(capsys) -> None:
    d = PlainStatusDisplay()
    d.register("X")
    d.print("Y", "msg")
    out = capsys.readouterr().out
    assert out == "\n[X] started\n\n[Y] msg\n"


def test_plain_cross_method_blank_line_print_then_remove(capsys) -> None:
    d = PlainStatusDisplay()
    d.print("X", "msg")
    d.remove("Y")
    out = capsys.readouterr().out
    assert out == "\n[X] msg\n\n[Y] finished\n"


def test_plain_named_to_anonymous_print_inserts_blank(capsys) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "hello")
    d.print("", "anon")
    out = capsys.readouterr().out
    assert out == "\n[Alice] hello\n\nanon\n"


def test_plain_anonymous_to_named_print_inserts_blank(capsys) -> None:
    d = PlainStatusDisplay()
    d.print("", "anon")
    d.print("Alice", "hello")
    out = capsys.readouterr().out
    assert out == "\nanon\n\n[Alice] hello\n"


def test_plain_same_caller_remove_after_print_no_blank(capsys) -> None:
    d = PlainStatusDisplay()
    d.print("X", "msg")
    d.remove("X")
    out = capsys.readouterr().out
    assert out == "\n[X] msg\n[X] finished\n"


# ── RecordingStatusDisplay behaviour ─────────────────────────────────────────


def test_recording_starts_empty() -> None:
    d = RecordingStatusDisplay()
    assert d.calls == []


def test_recording_captures_register() -> None:
    d = RecordingStatusDisplay()
    d.register("Plan")
    assert d.calls == [("register", "Plan", "started", "Setup")]


def test_recording_captures_register_with_initial_phase() -> None:
    d = RecordingStatusDisplay()
    d.register("Plan", startup_message="running", initial_phase="Planning")
    assert d.calls == [("register", "Plan", "running", "Planning")]


def test_recording_captures_remove() -> None:
    d = RecordingStatusDisplay()
    d.remove("Plan")
    assert d.calls == [("remove", "Plan", "finished", "success")]


def test_recording_captures_remove_with_custom_args() -> None:
    d = RecordingStatusDisplay()
    d.remove("Plan", shutdown_message="failed", shutdown_style="error")
    assert d.calls == [("remove", "Plan", "failed", "error")]


def test_recording_captures_print_new_api() -> None:
    d = RecordingStatusDisplay()
    d.print("Plan", "Planning complete.")
    assert d.calls == [("print", "Plan", "Planning complete.", None)]


def test_recording_captures_print_new_api_with_style() -> None:
    d = RecordingStatusDisplay()
    d.print("Plan", "error msg", style="error")
    assert d.calls == [("print", "Plan", "error msg", "error")]


def test_recording_captures_update_phase() -> None:
    d = RecordingStatusDisplay()
    d.update_phase("implementer-1", "Work")
    assert d.calls == [("update_phase", "implementer-1", "Work")]


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


def test_recording_print_produces_no_stdout(capsys) -> None:
    d = RecordingStatusDisplay()
    d.print("Plan", "Planning complete.")
    assert capsys.readouterr().out == ""


def test_recording_accumulates_multiple_new_api_calls() -> None:
    d = RecordingStatusDisplay()
    d.register("Plan")
    d.update_phase("Plan", "Work")
    d.remove("Plan")

    assert d.calls == [
        ("register", "Plan", "started", "Setup"),
        ("update_phase", "Plan", "Work"),
        ("remove", "Plan", "finished", "success"),
    ]
