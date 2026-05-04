import pytest

from pycastle.status_display import PlainStatusDisplay, StatusDisplay


def test_plain_status_display_satisfies_protocol() -> None:
    assert isinstance(PlainStatusDisplay(), StatusDisplay)


# --- Blank-line separator logic ---


def test_blank_line_before_first_output(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "hello")
    out = capsys.readouterr().out
    assert out == "\n[Alice] hello\n"


def test_print_no_blank_between_same_caller(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "first")
    d.print("Alice", "second")
    out = capsys.readouterr().out
    assert out == "\n[Alice] first\n[Alice] second\n"


def test_print_blank_when_caller_changes(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "hello")
    d.print("Bob", "world")
    out = capsys.readouterr().out
    assert out == "\n[Alice] hello\n\n[Bob] world\n"


def test_print_blank_for_anonymous_caller_even_if_previous_was_same(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.print("", "first")
    d.print("", "second")
    out = capsys.readouterr().out
    assert out == "\nfirst\n\nsecond\n"


def test_print_anonymous_caller_no_brackets(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.print("", "message")
    out = capsys.readouterr().out
    assert out == "\nmessage\n"


def test_print_named_caller_has_brackets(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "message")
    out = capsys.readouterr().out
    assert out == "\n[Alice] message\n"


def test_register_blank_when_caller_changes(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "hello")
    d.register("Bob", "agent")
    out = capsys.readouterr().out
    assert out == "\n[Alice] hello\n\n[Bob] started\n"


def test_register_blank_before_first_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.register("Alice", "agent")
    out = capsys.readouterr().out
    assert out == "\n[Alice] started\n"


def test_register_no_blank_same_caller_as_previous(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "hello")
    d.register("Alice", "agent")
    out = capsys.readouterr().out
    assert out == "\n[Alice] hello\n[Alice] started\n"


def test_register_updates_last_caller_for_subsequent_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.register("Alice", "agent")
    d.print("Alice", "message")
    out = capsys.readouterr().out
    assert out == "\n[Alice] started\n[Alice] message\n"


def test_remove_blank_when_caller_changes(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "hello")
    d.remove("Bob")
    out = capsys.readouterr().out
    assert out == "\n[Alice] hello\n\n[Bob] finished\n"


def test_remove_blank_before_first_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.remove("Alice")
    out = capsys.readouterr().out
    assert out == "\n[Alice] finished\n"


def test_remove_no_blank_same_caller_as_previous(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "hello")
    d.remove("Alice")
    out = capsys.readouterr().out
    assert out == "\n[Alice] hello\n[Alice] finished\n"


def test_remove_updates_last_caller_for_subsequent_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.remove("Alice")
    d.print("Alice", "message")
    out = capsys.readouterr().out
    assert out == "\n[Alice] finished\n[Alice] message\n"


def test_register_then_remove_same_caller_no_blank(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.register("Alice", "agent")
    d.remove("Alice")
    out = capsys.readouterr().out
    assert out == "\n[Alice] started\n[Alice] finished\n"


def test_print_anonymous_caller_after_named_caller_gets_blank(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "hello")
    d.print("", "anonymous")
    out = capsys.readouterr().out
    assert out == "\n[Alice] hello\n\nanonymous\n"


def test_register_anonymous_caller_blank_when_caller_changes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "hello")
    d.register("", "agent")
    out = capsys.readouterr().out
    assert out == "\n[Alice] hello\n\nstarted\n"


def test_remove_anonymous_caller_blank_when_caller_changes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "hello")
    d.remove("")
    out = capsys.readouterr().out
    assert out == "\n[Alice] hello\n\nfinished\n"


def test_print_named_caller_after_anonymous_gets_blank(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.print("", "anonymous")
    d.print("Alice", "named")
    out = capsys.readouterr().out
    assert out == "\nanonymous\n\n[Alice] named\n"


def test_register_blank_for_consecutive_anonymous_callers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.register("", "agent")
    d.register("", "agent")
    out = capsys.readouterr().out
    assert out == "\nstarted\n\nstarted\n"


def test_remove_blank_for_consecutive_anonymous_callers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.remove("")
    d.remove("")
    out = capsys.readouterr().out
    assert out == "\nfinished\n\nfinished\n"


def test_register_then_print_different_callers_blank(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.register("Alice", "agent")
    d.print("Bob", "message")
    out = capsys.readouterr().out
    assert out == "\n[Alice] started\n\n[Bob] message\n"


def test_register_then_remove_different_callers_blank(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.register("Alice", "agent")
    d.remove("Bob")
    out = capsys.readouterr().out
    assert out == "\n[Alice] started\n\n[Bob] finished\n"


def test_cross_method_consecutive_anonymous_blank(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.print("", "first")
    d.register("", "agent", "second")
    out = capsys.readouterr().out
    assert out == "\nfirst\n\nsecond\n"


def test_register_custom_startup_message(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.register("Alice", "agent", startup_message="connecting")
    out = capsys.readouterr().out
    assert out == "\n[Alice] connecting\n"


def test_register_initial_phase_does_not_affect_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.register("Alice", "agent", initial_phase="Running")
    out = capsys.readouterr().out
    assert out == "\n[Alice] started\n"


def test_register_work_body_does_not_affect_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.register("Alice", "agent", work_body="implementing issue #1")
    out = capsys.readouterr().out
    assert out == "\n[Alice] started\n"


def test_remove_custom_shutdown_message(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.remove("Alice", shutdown_message="aborted")
    out = capsys.readouterr().out
    assert out == "\n[Alice] aborted\n"


def test_full_lifecycle_interleaved_callers(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.register("Alice", "agent")
    d.print("Alice", "working")
    d.register("Bob", "agent")
    d.print("Bob", "running")
    d.remove("Alice")
    d.remove("Bob")
    out = capsys.readouterr().out
    assert out == (
        "\n"
        "[Alice] started\n"
        "[Alice] working\n"
        "\n"
        "[Bob] started\n"
        "[Bob] running\n"
        "\n"
        "[Alice] finished\n"
        "\n"
        "[Bob] finished\n"
    )


# --- Multi-line message splitting ---


def test_remove_multiline_message_emits_each_line_with_caller_prefix(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.remove("Alice", "line1\nline2\nline3")
    out = capsys.readouterr().out
    assert out == "\n[Alice] line1\n[Alice] line2\n[Alice] line3\n"


def test_print_multiline_message_emits_each_line_with_caller_prefix(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "line1\nline2")
    out = capsys.readouterr().out
    assert out == "\n[Alice] line1\n[Alice] line2\n"


def test_multiline_blank_before_fires_once_not_between_continuation_lines(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "hello")
    d.print("Bob", "line1\nline2")
    out = capsys.readouterr().out
    assert out == "\n[Alice] hello\n\n[Bob] line1\n[Bob] line2\n"


def test_remove_multiline_blank_before_fires_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "hello")
    d.remove("Bob", "line1\nline2")
    out = capsys.readouterr().out
    assert out == "\n[Alice] hello\n\n[Bob] line1\n[Bob] line2\n"


def test_print_multiline_anonymous_caller_emits_lines_without_brackets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.print("", "line1\nline2")
    out = capsys.readouterr().out
    assert out == "\nline1\nline2\n"


def test_print_multiline_same_caller_consecutive_no_blank_between_calls(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "a1\na2")
    d.print("Alice", "b1\nb2")
    out = capsys.readouterr().out
    assert out == "\n[Alice] a1\n[Alice] a2\n[Alice] b1\n[Alice] b2\n"


# --- kind-aware blank-line rules ---


def test_phase_to_agent_no_blank(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.register("Plan", "phase")
    d.register("Plan Agent", "agent")
    out = capsys.readouterr().out
    assert out == "\n[Plan] started\n[Plan Agent] started\n"


def test_agent_to_phase_no_blank(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.register("Plan", "phase")
    d.register("Plan Agent", "agent")
    d.remove("Plan Agent")
    d.remove("Plan")
    out = capsys.readouterr().out
    assert out == (
        "\n[Plan] started\n[Plan Agent] started\n[Plan Agent] finished\n[Plan] finished\n"
    )


def test_phase_to_different_phase_blank(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.register("Plan", "phase")
    d.register("Implement", "phase")
    out = capsys.readouterr().out
    assert out == "\n[Plan] started\n\n[Implement] started\n"


def test_agent_to_different_agent_blank(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.register("Implement Agent #1", "agent")
    d.register("Implement Agent #2", "agent")
    out = capsys.readouterr().out
    assert out == "\n[Implement Agent #1] started\n\n[Implement Agent #2] started\n"


def test_plan_lifecycle_end_to_end(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.register("Plan", "phase")
    d.register("Plan Agent", "agent")
    d.remove("Plan Agent")
    d.remove("Plan")
    d.register("Implement", "phase")
    out = capsys.readouterr().out
    assert out == (
        "\n"
        "[Plan] started\n"
        "[Plan Agent] started\n"
        "[Plan Agent] finished\n"
        "[Plan] finished\n"
        "\n"
        "[Implement] started\n"
    )


def test_anonymous_isolated_between_phase_and_agent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.register("Plan", "phase")
    d.print("", "anon")
    d.register("Plan Agent", "agent")
    out = capsys.readouterr().out
    assert out == ("\n[Plan] started\n\nanon\n\n[Plan Agent] started\n")


def test_print_unregistered_caller_blanks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.register("Plan", "phase")
    d.print("Stranger", "hi")
    out = capsys.readouterr().out
    assert out == "\n[Plan] started\n\n[Stranger] hi\n"
