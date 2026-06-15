from pycastle.display.status_print_sequencing import OutputEvent, StatusPrintSequencer


def test_first_output_event_requests_leading_blank_line() -> None:
    sequencer = StatusPrintSequencer()

    assert sequencer.record_output_event("Plan")


def test_output_event_recording_ignores_text_and_rendering_details() -> None:
    sequencer = StatusPrintSequencer()

    decisions = [
        sequencer.record_output_event(OutputEvent(caller="Plan", text="plain")),
        sequencer.record_output_event(
            OutputEvent(caller="Plan", text="[bold]rich markup[/bold]")
        ),
        sequencer.record_output_event(
            OutputEvent(caller="", text="\x1b[31manonymous ansi\x1b[0m")
        ),
        sequencer.record_output_event(OutputEvent(caller="", text="multi\nline")),
        sequencer.record_output_event(OutputEvent(caller="Implement", text="next")),
        sequencer.record_output_event(OutputEvent(caller="Review", text="switch")),
    ]

    assert decisions == [True, False, True, True, True, True]


def test_register_caller_records_kind() -> None:
    sequencer = StatusPrintSequencer()

    sequencer.register_caller("Plan", "phase")

    assert sequencer.caller_kind("Plan") == "phase"


def test_remove_caller_clears_registered_kind() -> None:
    sequencer = StatusPrintSequencer()
    sequencer.register_caller("Plan Agent", "agent")

    sequencer.remove_caller("Plan Agent")

    assert sequencer.caller_kind("Plan Agent") is None


def test_first_named_output_prepends_blank_line() -> None:
    sequencer = StatusPrintSequencer()

    assert sequencer.should_prepend_blank_line("Plan")


def test_repeated_named_output_does_not_prepend_blank_line() -> None:
    sequencer = StatusPrintSequencer()
    sequencer.register_caller("Plan", "phase")
    sequencer.record_output("Plan")

    assert not sequencer.should_prepend_blank_line("Plan")


def test_phase_to_agent_transition_does_not_prepend_blank_line() -> None:
    sequencer = StatusPrintSequencer()
    sequencer.register_caller("Plan", "phase")
    sequencer.record_output("Plan")
    sequencer.register_caller("Plan Agent", "agent")

    assert not sequencer.should_prepend_blank_line("Plan Agent")


def test_agent_to_phase_transition_does_not_prepend_blank_line() -> None:
    sequencer = StatusPrintSequencer()
    sequencer.register_caller("Plan", "phase")
    sequencer.register_caller("Plan Agent", "agent")
    sequencer.record_output("Plan Agent")

    assert not sequencer.should_prepend_blank_line("Plan")


def test_phase_to_different_phase_transition_prepends_blank_line() -> None:
    sequencer = StatusPrintSequencer()
    sequencer.register_caller("Plan", "phase")
    sequencer.record_output("Plan")
    sequencer.register_caller("Implement", "phase")

    assert sequencer.should_prepend_blank_line("Implement")


def test_anonymous_output_always_prepends_blank_line() -> None:
    sequencer = StatusPrintSequencer()
    sequencer.record_output("")

    assert sequencer.should_prepend_blank_line("")


def test_unregistered_named_output_after_phase_prepends_blank_line() -> None:
    sequencer = StatusPrintSequencer()
    sequencer.register_caller("Plan", "phase")
    sequencer.record_output("Plan")

    assert sequencer.should_prepend_blank_line("Stranger")
