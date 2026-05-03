import pytest

from pycastle.status_display import PlainStatusDisplay, StatusDisplay


def test_plain_status_display_satisfies_protocol() -> None:
    assert isinstance(PlainStatusDisplay(), StatusDisplay)


# --- Blank-line separator logic ---


def test_no_blank_line_before_first_output(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "hello")
    out = capsys.readouterr().out
    assert out == "[Alice] hello\n"


def test_print_no_blank_between_same_caller(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "first")
    d.print("Alice", "second")
    out = capsys.readouterr().out
    assert out == "[Alice] first\n[Alice] second\n"


def test_print_blank_when_caller_changes(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "hello")
    d.print("Bob", "world")
    out = capsys.readouterr().out
    assert out == "[Alice] hello\n\n[Bob] world\n"


def test_print_blank_for_anonymous_caller_even_if_previous_was_same(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.print("", "first")
    d.print("", "second")
    out = capsys.readouterr().out
    assert out == "first\n\nsecond\n"


def test_print_anonymous_caller_no_brackets(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.print("", "message")
    out = capsys.readouterr().out
    assert out == "message\n"


def test_print_named_caller_has_brackets(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "message")
    out = capsys.readouterr().out
    assert out == "[Alice] message\n"


def test_register_blank_when_caller_changes(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "hello")
    d.register("Bob")
    out = capsys.readouterr().out
    assert out == "[Alice] hello\n\n[Bob] started\n"


def test_register_no_blank_before_first_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.register("Alice")
    out = capsys.readouterr().out
    assert out == "[Alice] started\n"


def test_register_no_blank_same_caller_as_previous(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "hello")
    d.register("Alice")
    out = capsys.readouterr().out
    assert out == "[Alice] hello\n[Alice] started\n"


def test_register_updates_last_caller_for_subsequent_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.register("Alice")
    d.print("Alice", "message")
    out = capsys.readouterr().out
    assert out == "[Alice] started\n[Alice] message\n"


def test_remove_blank_when_caller_changes(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "hello")
    d.remove("Bob")
    out = capsys.readouterr().out
    assert out == "[Alice] hello\n\n[Bob] finished\n"


def test_remove_no_blank_before_first_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.remove("Alice")
    out = capsys.readouterr().out
    assert out == "[Alice] finished\n"


def test_remove_no_blank_same_caller_as_previous(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "hello")
    d.remove("Alice")
    out = capsys.readouterr().out
    assert out == "[Alice] hello\n[Alice] finished\n"


def test_remove_updates_last_caller_for_subsequent_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.remove("Alice")
    d.print("Alice", "message")
    out = capsys.readouterr().out
    assert out == "[Alice] finished\n[Alice] message\n"


def test_register_then_remove_same_caller_no_blank(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.register("Alice")
    d.remove("Alice")
    out = capsys.readouterr().out
    assert out == "[Alice] started\n[Alice] finished\n"


def test_print_anonymous_caller_after_named_caller_gets_blank(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "hello")
    d.print("", "anonymous")
    out = capsys.readouterr().out
    assert out == "[Alice] hello\n\nanonymous\n"


def test_register_anonymous_caller_blank_when_caller_changes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "hello")
    d.register("")
    out = capsys.readouterr().out
    assert out == "[Alice] hello\n\nstarted\n"


def test_remove_anonymous_caller_blank_when_caller_changes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.print("Alice", "hello")
    d.remove("")
    out = capsys.readouterr().out
    assert out == "[Alice] hello\n\nfinished\n"


def test_print_named_caller_after_anonymous_gets_blank(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.print("", "anonymous")
    d.print("Alice", "named")
    out = capsys.readouterr().out
    assert out == "anonymous\n\n[Alice] named\n"


def test_register_blank_for_consecutive_anonymous_callers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.register("")
    d.register("")
    out = capsys.readouterr().out
    assert out == "started\n\nstarted\n"


def test_remove_blank_for_consecutive_anonymous_callers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.remove("")
    d.remove("")
    out = capsys.readouterr().out
    assert out == "finished\n\nfinished\n"


def test_register_then_print_different_callers_blank(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.register("Alice")
    d.print("Bob", "message")
    out = capsys.readouterr().out
    assert out == "[Alice] started\n\n[Bob] message\n"


def test_register_then_remove_different_callers_blank(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.register("Alice")
    d.remove("Bob")
    out = capsys.readouterr().out
    assert out == "[Alice] started\n\n[Bob] finished\n"


def test_cross_method_consecutive_anonymous_blank(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.print("", "first")
    d.register("", "second")
    out = capsys.readouterr().out
    assert out == "first\n\nsecond\n"


def test_register_custom_startup_message(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.register("Alice", startup_message="connecting")
    out = capsys.readouterr().out
    assert out == "[Alice] connecting\n"


def test_register_initial_phase_does_not_affect_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()
    d.register("Alice", initial_phase="Running")
    out = capsys.readouterr().out
    assert out == "[Alice] started\n"


def test_remove_custom_shutdown_message(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.remove("Alice", shutdown_message="aborted")
    out = capsys.readouterr().out
    assert out == "[Alice] aborted\n"


def test_full_lifecycle_interleaved_callers(capsys: pytest.CaptureFixture[str]) -> None:
    d = PlainStatusDisplay()
    d.register("Alice")
    d.print("Alice", "working")
    d.register("Bob")
    d.print("Bob", "running")
    d.remove("Alice")
    d.remove("Bob")
    out = capsys.readouterr().out
    assert out == (
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
