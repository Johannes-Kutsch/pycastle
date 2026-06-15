import pytest

from pycastle.display.status_display import PlainStatusDisplay, StatusDisplay


def test_plain_status_display_satisfies_protocol() -> None:
    assert isinstance(PlainStatusDisplay(), StatusDisplay)


def test_print_anonymous_caller_no_brackets(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.print("", "message")
    assert capsys.readouterr().out == "\nmessage\n"


def test_print_named_caller_has_brackets(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "message")
    assert capsys.readouterr().out == "\n[Alice] message\n"


def test_print_ignores_style_argument(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "message", style="error")
    assert capsys.readouterr().out == "\n[Alice] message\n"


def test_register_custom_startup_message(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.register("Alice", "agent", startup_message="connecting")
    assert capsys.readouterr().out == "\n[Alice] connecting\n"


def test_register_initial_phase_does_not_affect_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.register("Alice", "agent", initial_phase="Running")
    assert capsys.readouterr().out == "\n[Alice] started\n"


def test_register_work_body_does_not_affect_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.register("Alice", "agent", work_body="implementing issue #1")
    assert capsys.readouterr().out == "\n[Alice] started\n"


def test_remove_custom_shutdown_message(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.remove("Alice", shutdown_message="aborted")
    assert capsys.readouterr().out == "\n[Alice] aborted\n"


def test_remove_multiline_message_emits_each_line_with_caller_prefix(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.remove("Alice", "line1\nline2\nline3")
    assert capsys.readouterr().out == "\n[Alice] line1\n[Alice] line2\n[Alice] line3\n"


def test_print_multiline_message_emits_each_line_with_caller_prefix(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "line1\nline2")
    assert capsys.readouterr().out == "\n[Alice] line1\n[Alice] line2\n"


def test_print_multiline_anonymous_caller_emits_lines_without_brackets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.print("", "line1\nline2")
    assert capsys.readouterr().out == "\nline1\nline2\n"


def test_phase_to_agent_smoke_uses_shared_sequencing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.register("Plan", "phase")
    d.register("Plan Agent", "agent")
    assert capsys.readouterr().out == "\n[Plan] started\n[Plan Agent] started\n"
