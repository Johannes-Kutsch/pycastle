from pycastle.iteration._deps import RecordingLogger


def test_log_error_is_recorded():
    logger = RecordingLogger()
    issue = {"number": 1, "title": "Fix bug"}
    error = ValueError("something went wrong")

    logger.log_error(issue, error)

    assert logger.errors == [(issue, error)]


def test_log_agent_output_is_recorded():
    logger = RecordingLogger()

    logger.log_agent_output("implementer", "some output")

    assert logger.agent_outputs == [("implementer", "some output")]


def test_multiple_log_error_calls_accumulate():
    logger = RecordingLogger()
    issue1 = {"number": 1}
    issue2 = {"number": 2}
    error1 = RuntimeError("first")
    error2 = RuntimeError("second")

    logger.log_error(issue1, error1)
    logger.log_error(issue2, error2)

    assert logger.errors == [(issue1, error1), (issue2, error2)]


def test_multiple_log_agent_output_calls_accumulate():
    logger = RecordingLogger()

    logger.log_agent_output("planner", "plan output")
    logger.log_agent_output("implementer", "impl output")

    assert logger.agent_outputs == [
        ("planner", "plan output"),
        ("implementer", "impl output"),
    ]


def test_starts_with_no_records():
    logger = RecordingLogger()

    assert logger.errors == []
    assert logger.agent_outputs == []
