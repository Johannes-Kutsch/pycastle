from pycastle.status_display import PlainStatusDisplay, StatusDisplay


# ── Protocol conformance ──────────────────────────────────────────────────────


def test_plain_status_display_satisfies_protocol() -> None:
    assert isinstance(PlainStatusDisplay(), StatusDisplay)


# ── print behaviour ───────────────────────────────────────────────────────────


def test_plain_print_routes_to_stdout(capsys) -> None:
    d = PlainStatusDisplay()
    d.print("Planning complete. 3 issue(s)")
    assert capsys.readouterr().out == "Planning complete. 3 issue(s)\n"


# ── panel method no-ops ───────────────────────────────────────────────────────


def test_plain_add_agent_produces_no_output(capsys) -> None:
    d = PlainStatusDisplay()
    d.add_agent("implementer-1", "Setup")
    assert capsys.readouterr().out == ""


def test_plain_add_agent_with_work_body_produces_no_output(capsys) -> None:
    d = PlainStatusDisplay()
    d.add_agent("implementer-1", "Work", "working on auth bug")
    assert capsys.readouterr().out == ""


def test_plain_update_phase_produces_no_output(capsys) -> None:
    d = PlainStatusDisplay()
    d.update_phase("implementer-1", "Work")
    assert capsys.readouterr().out == ""


def test_plain_remove_agent_produces_no_output(capsys) -> None:
    d = PlainStatusDisplay()
    d.remove_agent("implementer-1")
    assert capsys.readouterr().out == ""


def test_plain_reset_idle_timer_produces_no_output(capsys) -> None:
    d = PlainStatusDisplay()
    d.reset_idle_timer("implementer-1")
    assert capsys.readouterr().out == ""
