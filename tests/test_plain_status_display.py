from pycastle.status_display import PlainStatusDisplay, StatusDisplay


# ── Protocol conformance ──────────────────────────────────────────────────────


def test_plain_status_display_satisfies_protocol() -> None:
    assert isinstance(PlainStatusDisplay(), StatusDisplay)


# ── print behaviour ───────────────────────────────────────────────────────────


def test_plain_print_with_caller_outputs_bracketed_line(capsys) -> None:
    d = PlainStatusDisplay()
    d.print("Plan", "Planning complete. 3 issue(s)")
    assert capsys.readouterr().out == "[Plan] Planning complete. 3 issue(s)\n"


def test_plain_print_with_empty_caller_outputs_message_verbatim(capsys) -> None:
    d = PlainStatusDisplay()
    d.print("", "no prefix here")
    assert capsys.readouterr().out == "no prefix here\n"


def test_plain_print_accepts_non_string_message(capsys) -> None:
    d = PlainStatusDisplay()
    d.print("X", 42)
    assert capsys.readouterr().out == "[X] 42\n"


# ── panel method no-ops ───────────────────────────────────────────────────────


def test_plain_update_phase_produces_no_output(capsys) -> None:
    d = PlainStatusDisplay()
    d.update_phase("implementer-1", "Work")
    assert capsys.readouterr().out == ""


def test_plain_reset_idle_timer_produces_no_output(capsys) -> None:
    d = PlainStatusDisplay()
    d.reset_idle_timer("implementer-1")
    assert capsys.readouterr().out == ""
