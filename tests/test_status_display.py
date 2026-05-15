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
    d.register("Plan Agent", "agent")
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


def test_rich_agent_without_issue_number_sorts_before_agent_with_issue_number() -> None:
    d = RichStatusDisplay()
    d.register("Implement Agent #5", "agent")
    d.register("Plan Agent", "agent")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert output.find("Plan Agent") < output.find("Implement Agent #5")
    d.stop()


def test_rich_implementers_render_sorted_by_issue_number() -> None:
    d = RichStatusDisplay()
    d.register("Implement Agent #42", "agent")
    d.register("Implement Agent #7", "agent")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert output.find("Implement Agent #7") < output.find("Implement Agent #42")
    d.stop()


def test_rich_agent_with_unknown_name_sorts_before_agent_with_issue_number() -> None:
    d = RichStatusDisplay()
    d.register("Implement Agent #1", "agent")
    d.register("Unknown-agent", "agent")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert output.find("Unknown-agent") < output.find("Implement Agent #1")
    d.stop()


def test_rich_renders_agent_name_without_dash_suffix() -> None:
    d = RichStatusDisplay()
    d.register("Implement Agent #42", "agent")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert "Implement Agent #42" in output
    assert " - " not in output
    d.stop()


def test_rich_renders_no_header_row() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert "Idle (s)" not in output
    assert "Log" not in output
    d.stop()


def test_rich_renders_blank_line_before_agent_rows() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    planner_pos = output.find("Plan Agent")
    lines_before = output[:planner_pos].split("\n")
    assert any(line.strip() == "" for line in lines_before)
    d.stop()


def test_rich_elapsed_format_shows_seconds_under_one_minute() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent")

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
    d.register("Plan Agent", "agent")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert "5m 12s" in output
    assert "42s" in output
    d.stop()


def test_rich_phase_appears_in_output() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent")
    d.update_phase("Plan Agent", "DESIGNING")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert "DESIGNING" in output
    d.stop()


def test_rich_agent_names_are_right_aligned_by_elapsed_column(monkeypatch) -> None:
    import pycastle.rich_status_display as mod

    # #7 started at t=0, #42 started at t=270; render at t=312
    # elapsed #7 = 5m 12s, elapsed #42 = 42s — different widths.
    # Rich right-aligns the elapsed column naturally, so both agent names
    # start at the same column regardless of fixed-width enforcement.
    times = iter([0.0, 270.0] + [312.0] * 20)
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(times))

    d = RichStatusDisplay()
    d.register("Implement Agent #7", "agent")
    d.register("Implement Agent #42", "agent")

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
    d.register("Plan Agent", "agent")

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
    d.register("Plan Agent", "agent")
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
    d.register("Review Agent #42", "agent")
    d.register("Review Agent #7", "agent")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()

    assert output.find("Review Agent #7") < output.find("Review Agent #42")
    d.stop()


def test_rich_register_twice_with_same_name_is_safe() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent")
    d.register("Plan Agent", "agent")

    console = Console(record=True, width=200)
    console.print(d)
    lines = [ln for ln in console.export_text().splitlines() if "Plan Agent" in ln]
    assert len(lines) == 1
    d.stop()


def test_rich_agent_name_renders_without_hyperlink() -> None:
    d = RichStatusDisplay()
    d.register("Implement Agent #5", "agent")

    ansi = _ansi_output(d)
    d.stop()

    assert "\x1b]8" not in ansi


# ── Body column tests ────────────────────────────────────────────────────────


def test_rich_body_shows_lifecycle_phase_during_non_work() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent", work_body="Creating Plan from 3 issues")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()
    d.stop()

    assert "Setup" in output
    assert "Creating Plan from 3 issues" not in output


def test_rich_body_shows_work_body_during_work() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent", work_body="Creating Plan from 3 issues")
    d.update_phase("Plan Agent", "Work")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()
    d.stop()

    assert "Creating Plan from 3 issues" in output
    assert "Work" not in output


def test_rich_body_is_unstyled() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent")

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
    d.register("Plan Agent", "agent")
    d.update_phase("Plan Agent", "Work")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()
    d.stop()

    assert "Plan Agent" in output
    assert "Work" not in output


def test_rich_body_shows_work_body_after_phase_transitions_to_work() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent", work_body="Creating Plan from 3 issues")
    d.update_phase("Plan Agent", "Work")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()
    d.stop()

    assert "Creating Plan from 3 issues" in output
    assert "Setup" not in output


def test_rich_body_reverts_to_phase_name_after_transitioning_from_work() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent", work_body="Creating Plan from 3 issues")
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
    d.register("Preflight", "agent", initial_phase="Running")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()
    d.stop()

    assert "Running" in output
    assert "Setup" not in output


def test_rich_pre_flight_agent_sorts_before_implementers() -> None:
    d = RichStatusDisplay()
    d.register("Implement Agent #1", "agent")
    d.register("Preflight Agent", "agent")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()
    d.stop()

    assert output.find("Preflight Agent") < output.find("Implement Agent")


# ── Column order tests ───────────────────────────────────────────────────────


def test_rich_tokens_column_appears_before_name() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent")
    d.update_tokens("Plan Agent", 78_300)

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()
    d.stop()

    assert output.index("78.3k") < output.index("Plan Agent")


# ── Token column tests ────────────────────────────────────────────────────────


def _truecolor_output(display: RichStatusDisplay) -> str:
    buf = io.StringIO()
    console = Console(
        file=buf, width=200, force_terminal=True, color_system="truecolor"
    )
    console.print(display)
    return buf.getvalue()


def test_rich_token_column_blank_when_no_tokens_set() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()
    d.stop()

    assert not re.search(r"\d+\.\d+k", output)


def test_rich_token_column_shows_current_after_update() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent")
    d.update_tokens("Plan Agent", 78_300)

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()
    d.stop()

    assert "78.3k" in output


def test_rich_token_column_shows_peak_with_arrow() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent")
    d.update_tokens("Plan Agent", 92_100)
    d.update_tokens("Plan Agent", 78_000)

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()
    d.stop()

    assert "78.0k" in output
    assert "92.1k" in output
    assert "↑" in output


def test_rich_token_peak_is_monotonic() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent")
    d.update_tokens("Plan Agent", 100_000)
    d.update_tokens("Plan Agent", 50_000)

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()
    d.stop()

    assert "50.0k" in output
    assert "100.0k" in output


def test_rich_update_tokens_for_unknown_agent_is_safe() -> None:
    d = RichStatusDisplay()
    d.update_tokens("never-added", 50_000)


def test_rich_token_above_80k_renders_gold_color() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent")
    d.update_tokens("Plan Agent", 85_000)

    ansi = _truecolor_output(d)
    d.stop()

    assert "212;168;67" in ansi


def test_rich_token_above_100k_renders_coral_color() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent")
    d.update_tokens("Plan Agent", 110_000)

    ansi = _truecolor_output(d)
    d.stop()

    assert "217;119;87" in ansi


def test_rich_token_at_exactly_80k_has_no_special_color() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent")
    d.update_tokens("Plan Agent", 80_000)

    ansi = _truecolor_output(d)
    d.stop()

    assert "212;168;67" not in ansi
    assert "217;119;87" not in ansi


def test_rich_token_at_exactly_100k_renders_gold_not_coral() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent")
    d.update_tokens("Plan Agent", 100_000)

    ansi = _truecolor_output(d)
    d.stop()

    assert "212;168;67" in ansi
    assert "217;119;87" not in ansi


def test_rich_token_current_and_peak_colored_independently() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent")
    d.update_tokens("Plan Agent", 120_000)
    d.update_tokens("Plan Agent", 50_000)

    ansi = _truecolor_output(d)
    d.stop()

    plain = re.sub(r"\x1b\[[^m]*m", "", ansi)
    assert plain.count("120.0k") == 1
    assert "217;119;87" in ansi
    assert "212;168;67" not in ansi


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
    d.register("Plan Agent", "agent")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 1)  # bold


def test_rich_digit_sequences_in_agent_name_render_cyan() -> None:
    d = RichStatusDisplay()
    d.register("Implement Agent #5", "agent")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 36)  # cyan


def test_rich_non_numeric_agent_name_renders_bold_without_cyan() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 1)  # bold
    assert not _has_code(ansi, 36)  # no cyan


def test_rich_elapsed_column_renders_dim() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent")

    ansi = _ansi_output(d)
    d.stop()

    # Elapsed column precedes the agent name — dim must appear before "Plan Agent"
    before_name = ansi[: ansi.index("Plan Agent")]
    assert _has_code(before_name, 2)  # dim


def test_rich_idle_column_renders_dim() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent")

    ansi = _ansi_output(d)
    d.stop()

    # Idle column follows the agent name — dim must appear after "Plan Agent"
    after_name = ansi[ansi.index("Plan Agent") + len("Plan Agent") :]
    assert _has_code(after_name, 2)  # dim


def test_rich_role_name_renders_without_color_for_unknown_agent() -> None:
    d = RichStatusDisplay()
    d.register("Unknown-agent", "agent")

    ansi = _ansi_output(d)
    d.stop()

    assert not _has_code(ansi, 34)  # no blue
    assert not _has_code(ansi, 214)  # no orange1
    assert not _has_code(ansi, 33)  # no yellow
    assert not _has_code(ansi, 32)  # no green


def test_rich_planner_name_renders_bold_without_role_color() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 1)  # bold
    assert not _has_code(ansi, 34)  # no blue


def test_rich_implementer_name_renders_bold_without_role_color() -> None:
    d = RichStatusDisplay()
    d.register("Implement Agent #5", "agent")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 1)  # bold
    assert not _has_code(ansi, 214)  # no orange1


def test_rich_reviewer_name_renders_bold_without_role_color() -> None:
    d = RichStatusDisplay()
    d.register("Review Agent #3", "agent")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 1)  # bold
    assert not _has_code(ansi, 33)  # no yellow


def test_rich_merger_name_renders_bold_without_role_color() -> None:
    d = RichStatusDisplay()
    d.register("Merge Agent", "agent")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 1)  # bold
    assert not _has_code(ansi, 32)  # no green


def test_rich_pre_flight_name_renders_bold_without_role_color() -> None:
    d = RichStatusDisplay()
    d.register("Preflight Agent", "agent")

    ansi = _ansi_output(d)
    d.stop()

    assert _has_code(ansi, 1)  # bold
    assert not _has_code(ansi, 129)  # no purple


def test_rich_pre_flight_reporter_name_renders_bold_without_role_color() -> None:
    d = RichStatusDisplay()
    d.register("Pre-Flight Reporter", "agent")

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
    d.register("X", "agent")
    d.stop()
    assert "[X] started" in capsys.readouterr().out


def test_rich_new_api_register_custom_startup_message(capsys) -> None:
    d = RichStatusDisplay()
    d.register("X", "agent", startup_message="booting")
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
    d.register("Implement Agent #3", "agent")
    d.register("Implement Agent #1", "agent")
    d.register("Plan Agent", "agent")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()
    d.stop()

    assert output.find("Plan Agent") < output.find("Implement Agent #1")
    assert output.find("Implement Agent #1") < output.find("Implement Agent #3")


def test_rich_preflight_phase_row_renders_above_plan_agent() -> None:
    d = RichStatusDisplay()
    d.register("Plan Agent", "agent")
    d.register("Preflight", "phase")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()
    d.stop()

    assert output.find("Preflight") < output.find("Plan Agent")


def test_rich_improve_phase_row_renders_above_scan_agent() -> None:
    d = RichStatusDisplay()
    d.register("Scan Agent", "agent")
    d.register("Improve", "phase")

    console = Console(record=True, width=200)
    console.print(d)
    output = console.export_text()
    d.stop()

    assert output.find("Improve") < output.find("Scan Agent")


def test_rich_new_api_first_print_has_leading_blank_line(capsys) -> None:
    d = RichStatusDisplay()
    d.print("A", "hello")
    out = capsys.readouterr().out
    assert out.startswith("\n") and "[A] hello" in out


def test_rich_register_with_empty_caller_prints_message_only(capsys) -> None:
    d = RichStatusDisplay()
    d.register("", "agent", startup_message="booting")
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
    d.register("A", "agent")
    d.register("B", "agent")
    d.stop()
    out = capsys.readouterr().out
    assert "[A] started" in out
    assert "[B] started" in out


def test_rich_register_agent_to_agent_no_blank(capsys) -> None:
    d = RichStatusDisplay()
    d.register("X", "agent")
    d.register("Y", "agent")
    d.stop()
    out = capsys.readouterr().out
    assert "[X] started\n[Y] started" in out


def test_rich_register_blank_line_before_first_output(capsys) -> None:
    d = RichStatusDisplay()
    d.register("X", "agent")
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
    d.register("X", "agent")
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
    d.register("X", "agent")
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
    d.register("Plan", "agent", startup_message="started")
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


def test_rich_print_multiline_emits_each_line_with_caller_prefix(capsys) -> None:
    d = RichStatusDisplay()
    d.print("Alice", "line1\nline2")
    out = capsys.readouterr().out
    assert "[Alice] line1" in out
    assert "[Alice] line2" in out


def test_rich_print_multiline_blank_before_fires_once(capsys) -> None:
    d = RichStatusDisplay()
    d.print("Alice", "hello")
    d.print("Bob", "line1\nline2")
    out = capsys.readouterr().out
    assert "hello\n\n[Bob] line1\n[Bob] line2" in out


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
    d.register("X", "agent")
    d.register("X", "agent")
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
    d.register("", "agent", "anon start")
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


def test_plain_update_tokens_produces_no_output(capsys) -> None:
    d = PlainStatusDisplay()
    d.update_tokens("implementer-1", 50_000)
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
    d.register("X", "agent")
    assert capsys.readouterr().out == "\n[X] started\n"


def test_plain_register_with_custom_startup_message(capsys) -> None:
    d = PlainStatusDisplay()
    d.register("X", "agent", startup_message="custom")
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
    d.register("", "agent", startup_message="booting")
    assert capsys.readouterr().out == "\nbooting\n"


def test_plain_remove_with_empty_caller_prints_message_only(capsys) -> None:
    d = PlainStatusDisplay()
    d.remove("", shutdown_message="done")
    assert capsys.readouterr().out == "\ndone\n"


def test_plain_register_agent_to_agent_no_blank(capsys) -> None:
    d = PlainStatusDisplay()
    d.register("X", "agent")
    d.register("Y", "agent")
    out = capsys.readouterr().out
    assert out == "\n[X] started\n[Y] started\n"


def test_plain_register_blank_line_before_first_output(capsys) -> None:
    d = PlainStatusDisplay()
    d.register("X", "agent")
    out = capsys.readouterr().out
    assert out.startswith("\n") and "[X] started" in out


def test_plain_register_no_blank_line_on_same_caller(capsys) -> None:
    d = PlainStatusDisplay()
    d.register("X", "agent")
    d.register("X", "agent")
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
    d.register("X", "agent")
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
    d.register("Plan", "phase")
    assert d.calls == [("register", "Plan", "phase", "started", "Setup")]


def test_recording_captures_register_with_initial_phase() -> None:
    d = RecordingStatusDisplay()
    d.register("Plan", "phase", startup_message="running", initial_phase="Planning")
    assert d.calls == [("register", "Plan", "phase", "running", "Planning")]


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


def test_recording_captures_update_tokens() -> None:
    d = RecordingStatusDisplay()
    d.update_tokens("Plan Agent", 78_300)
    assert d.calls == [("update_tokens", "Plan Agent", 78_300)]


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
    d.register("Plan", "phase")
    d.update_phase("Plan", "Work")
    d.remove("Plan")

    assert d.calls == [
        ("register", "Plan", "phase", "started", "Setup"),
        ("update_phase", "Plan", "Work"),
        ("remove", "Plan", "finished", "success"),
    ]


# --- kind-aware blank-line rules (Rich) ---


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def test_rich_phase_to_agent_no_blank(capsys) -> None:
    d = RichStatusDisplay()
    d.register("Plan", "phase")
    d.register("Plan Agent", "agent")
    out = _strip_ansi(capsys.readouterr().out)
    assert "[Plan] started\n[Plan Agent] started" in out
    d.stop()


def test_rich_agent_to_phase_no_blank(capsys) -> None:
    d = RichStatusDisplay()
    d.register("Plan", "phase")
    d.register("Plan Agent", "agent")
    d.remove("Plan Agent")
    d.remove("Plan")
    out = _strip_ansi(capsys.readouterr().out)
    assert "[Plan Agent] finished\n[Plan] finished" in out


def test_rich_phase_to_different_phase_blank(capsys) -> None:
    d = RichStatusDisplay()
    d.register("Plan", "phase")
    d.register("Implement", "phase")
    out = _strip_ansi(capsys.readouterr().out)
    assert "[Plan] started\n\n[Implement] started" in out
    d.stop()


def test_rich_agent_to_different_agent_no_blank(capsys) -> None:
    d = RichStatusDisplay()
    d.register("Implement Agent #1", "agent")
    d.register("Implement Agent #2", "agent")
    out = _strip_ansi(capsys.readouterr().out)
    assert "[Implement Agent #1] started\n[Implement Agent #2] started" in out
    d.stop()


def test_rich_plan_lifecycle_end_to_end(capsys) -> None:
    d = RichStatusDisplay()
    d.register("Plan", "phase")
    d.register("Plan Agent", "agent")
    d.remove("Plan Agent")
    d.remove("Plan")
    d.register("Implement", "phase")
    out = _strip_ansi(capsys.readouterr().out)
    expected = (
        "[Plan] started\n"
        "[Plan Agent] started\n"
        "[Plan Agent] finished\n"
        "[Plan] finished\n"
        "\n"
        "[Implement] started"
    )
    assert expected in out
    d.stop()


def test_rich_anonymous_isolated_between_phase_and_agent(capsys) -> None:
    d = RichStatusDisplay()
    d.register("Plan", "phase")
    d.print("", "anon")
    d.register("Plan Agent", "agent")
    out = _strip_ansi(capsys.readouterr().out)
    assert "[Plan] started\n\nanon\n\n[Plan Agent] started" in out
    d.stop()


def test_rich_print_unregistered_caller_blanks(capsys) -> None:
    d = RichStatusDisplay()
    d.register("Plan", "phase")
    d.print("Stranger", "hi")
    out = _strip_ansi(capsys.readouterr().out)
    assert "[Plan] started\n\n[Stranger] hi" in out
    d.stop()


# ── Agent palette coloring ────────────────────────────────────────────────────


def _truecolor_print_output(caller: str, message: str) -> str:
    """Capture a single .print() call on a fresh display with truecolor ANSI."""
    buf = io.StringIO()
    console = Console(
        file=buf, width=200, force_terminal=True, color_system="truecolor"
    )
    d = RichStatusDisplay(console=console)
    d.register(caller, "agent")
    buf.seek(0)
    buf.truncate(0)
    d.print(caller, message)
    d.stop()
    return buf.getvalue()


def test_rich_agent_with_issue_number_prefix_renders_in_palette_color() -> None:
    """[Caller] prefix for an agent with #N uses palette[N % 9] truecolor."""
    # N=9 → 9%9=0 → palette[0] deep purple
    ansi = _truecolor_print_output("Implement Agent #9", "hello")

    bracket_idx = ansi.find("[Implement Agent #9]")
    assert bracket_idx >= 0, "bracketed prefix not found in output"
    # palette[0] is deeply-saturated purple: rgb(149, 97, 226)
    assert "149;97;226" in ansi[:bracket_idx], "palette color not found before prefix"


def test_rich_agent_name_column_in_table_renders_in_palette_color() -> None:
    """Name column in live table for agent with #N uses palette[N % 9] truecolor."""
    d = RichStatusDisplay()
    d.register("Implement Agent #9", "agent")  # N=9 → 9%9=0 → palette[0] deep purple

    buf = io.StringIO()
    console = Console(
        file=buf, width=200, force_terminal=True, color_system="truecolor"
    )
    console.print(d)
    ansi = buf.getvalue()
    d.stop()

    name_idx = ansi.find("Implement Agent #")
    assert name_idx >= 0
    before_name = ansi[:name_idx]
    assert "149;97;226" in before_name, "palette color not found before name in table"


def test_rich_same_issue_number_gives_same_prefix_color() -> None:
    """Implement Agent #715 and Review Agent #715 get the same [Caller] color."""
    impl_ansi = _truecolor_print_output("Implement Agent #715", "hello")
    review_ansi = _truecolor_print_output("Review Agent #715", "hello")

    def color_before_bracket(ansi: str, bracket_text: str) -> str:
        idx = ansi.find(bracket_text)
        assert idx >= 0
        return ansi[:idx]

    impl_before = color_before_bracket(impl_ansi, "[Implement Agent #715]")
    review_before = color_before_bracket(review_ansi, "[Review Agent #715]")
    # Both must contain the same RGB triple (palette[715 % 9])
    rgb_pattern = re.compile(r"(\d+;\d+;\d+)")
    impl_rgb = rgb_pattern.search(impl_before)
    review_rgb = rgb_pattern.search(review_before)
    assert impl_rgb and review_rgb
    assert impl_rgb.group(1) == review_rgb.group(1)


def _prefix_ansi_style(caller: str, message: str) -> str:
    """Capture the raw ANSI escape sequence applied to the [caller] prefix."""
    ansi = _truecolor_print_output(caller, message)
    m = re.search(rf"(\x1b\[[\d;]*m)\[{re.escape(caller)}\]", ansi)
    assert m, f"could not find styled prefix for {caller!r} in {ansi!r}"
    return m.group(1)


def _prefix_rgb(caller: str) -> tuple[int, int, int]:
    """Parse the truecolor RGB used for the [caller] prefix from rendered output."""
    ansi = _truecolor_print_output(caller, "hello")
    idx = ansi.find(f"[{caller}]")
    assert idx >= 0
    m = re.search(r"38;2;(\d+);(\d+);(\d+)", ansi[:idx])
    assert m, f"no truecolor RGB found before prefix in {ansi!r}"
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def test_rich_palette_index_0_renders_deeply_saturated_purple() -> None:
    # N=9 → 9 % 9 = 0
    assert _prefix_rgb("Implement Agent #9") == (149, 97, 226)


def test_rich_palette_index_1_renders_deeply_saturated_orange() -> None:
    # N=10 → 10 % 9 = 1
    assert _prefix_rgb("Implement Agent #10") == (255, 140, 50)


def test_rich_palette_index_2_renders_deeply_saturated_yellow() -> None:
    # N=11 → 11 % 9 = 2
    assert _prefix_rgb("Implement Agent #11") == (240, 205, 45)


def test_rich_consecutive_issue_numbers_render_in_different_styles() -> None:
    """Adjacent #N values must render the [Caller] prefix with distinct styles."""
    # A full palette cycle: each pair of adjacent Ns crosses a hue family.
    styles = [_prefix_ansi_style(f"Implement Agent #{n}", "hello") for n in range(10)]
    for n in range(len(styles) - 1):
        assert styles[n] != styles[n + 1], (
            f"#{n} and #{n + 1} render with identical style {styles[n]!r}"
        )


def _has_truecolor_rgb(ansi: str) -> bool:
    """Return True if the string contains any truecolor RGB escape (38;2;R;G;B)."""
    return bool(re.search(r"\x1b\[(?:\d+;)*38;2;\d+;\d+;\d+m", ansi))


def test_rich_phase_caller_prefix_has_no_palette_color() -> None:
    """Phase callers (no #N) render [Caller] in bold only — no truecolor."""
    ansi = _truecolor_print_output("Implement", "doing work")

    bracket_idx = ansi.find("[Implement]")
    assert bracket_idx >= 0
    assert not _has_truecolor_rgb(ansi[:bracket_idx]), (
        "unexpected palette color on phase prefix"
    )


def test_rich_agent_without_issue_number_prefix_has_no_palette_color() -> None:
    """Agents without #N (e.g. Plan Agent) render [Caller] in bold only."""
    ansi = _truecolor_print_output("Plan Agent", "planning")

    bracket_idx = ansi.find("[Plan Agent]")
    assert bracket_idx >= 0
    assert not _has_truecolor_rgb(ansi[:bracket_idx]), (
        "unexpected palette color on Plan Agent prefix"
    )


def test_rich_agent_with_issue_number_shutdown_style_still_applies() -> None:
    """success/error style on body is not suppressed by agent palette coloring."""
    buf, console = _make_ansi_console()
    d = RichStatusDisplay(console=console)
    d.remove("Implement Agent #9", shutdown_message="done", shutdown_style="success")
    ansi = buf.getvalue()

    bracket_idx = ansi.find("[Implement Agent #9]")
    assert bracket_idx >= 0
    # Green (32) must appear before the bracket (stylize applies to whole text)
    assert _has_code(ansi[:bracket_idx], 32), "success green not applied to prefix"


def test_rich_agent_table_digit_segments_retain_cyan_alongside_palette_color() -> None:
    """Digit segments in table name column are still cyan even with palette color active."""
    d = RichStatusDisplay()
    d.register("Implement Agent #9", "agent")

    buf = io.StringIO()
    console = Console(file=buf, width=200, force_terminal=True, color_system="256")
    console.print(d)
    ansi = buf.getvalue()
    d.stop()

    name_idx = ansi.find("Implement Agent #")
    assert name_idx >= 0
    after_name_start = ansi[name_idx:]
    # cyan (36) should appear in the segment containing the digits
    assert _has_code(after_name_start, 36), "cyan not present in digit segment"


def test_plain_status_display_agent_with_issue_number_emits_no_ansi(capsys) -> None:
    """PlainStatusDisplay never emits ANSI codes, even for agents with #N."""
    d = PlainStatusDisplay()
    d.register("Implement Agent #9", "agent")
    d.print("Implement Agent #9", "hello")
    d.remove("Implement Agent #9", shutdown_message="done")
    out = capsys.readouterr().out

    assert "\x1b[" not in out, "ANSI escape found in PlainStatusDisplay output"
