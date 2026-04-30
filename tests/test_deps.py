import asyncio

import pytest

from pycastle.agent_result import PreflightFailure
from pycastle.iteration._deps import FakeAgentRunner, RecordingLogger


@pytest.fixture
def logger() -> RecordingLogger:
    return RecordingLogger()


def test_starts_with_no_records(logger: RecordingLogger) -> None:
    assert logger.errors == []
    assert logger.agent_outputs == []


def test_log_error_is_recorded(logger: RecordingLogger) -> None:
    issue = {"number": 1, "title": "Fix bug"}
    error = ValueError("something went wrong")

    logger.log_error(issue, error)

    assert logger.errors == [(issue, error)]


def test_log_error_records_preflight_failure(logger: RecordingLogger) -> None:
    issue = {"number": 42}
    failure = PreflightFailure(
        failures=(("ruff check", "ruff check .", "E501 line too long"),)
    )

    logger.log_error(issue, failure)

    assert logger.errors == [(issue, failure)]


def test_log_agent_output_is_recorded(logger: RecordingLogger) -> None:
    logger.log_agent_output("implementer", "some output")

    assert logger.agent_outputs == [("implementer", "some output")]


def test_multiple_log_error_calls_accumulate(logger: RecordingLogger) -> None:
    issue1 = {"number": 1}
    issue2 = {"number": 2}
    error1 = RuntimeError("first")
    error2 = RuntimeError("second")

    logger.log_error(issue1, error1)
    logger.log_error(issue2, error2)

    assert logger.errors == [(issue1, error1), (issue2, error2)]


def test_multiple_log_agent_output_calls_accumulate(logger: RecordingLogger) -> None:
    logger.log_agent_output("planner", "plan output")
    logger.log_agent_output("implementer", "impl output")

    assert logger.agent_outputs == [
        ("planner", "plan output"),
        ("implementer", "impl output"),
    ]


# --- FakeAgentRunner ---


@pytest.fixture
def prompt_file(tmp_path):
    return tmp_path / "prompt.md"


@pytest.fixture
def mount_path(tmp_path):
    return tmp_path


def test_fake_agent_runner_starts_with_no_calls() -> None:
    runner = FakeAgentRunner(responses=["output"])

    assert runner.calls == []


def test_fake_agent_runner_returns_queued_response(prompt_file, mount_path) -> None:
    runner = FakeAgentRunner(responses=["agent output"])

    result = asyncio.run(
        runner.run(name="planner", prompt_file=prompt_file, mount_path=mount_path)
    )

    assert result == "agent output"


def test_fake_agent_runner_returns_responses_in_order(prompt_file, mount_path) -> None:
    runner = FakeAgentRunner(responses=["first", "second", "third"])

    async def _run():
        return [
            await runner.run(
                name="planner", prompt_file=prompt_file, mount_path=mount_path
            ),
            await runner.run(
                name="implementer", prompt_file=prompt_file, mount_path=mount_path
            ),
            await runner.run(
                name="merger", prompt_file=prompt_file, mount_path=mount_path
            ),
        ]

    assert asyncio.run(_run()) == ["first", "second", "third"]


def test_fake_agent_runner_records_call_arguments(prompt_file, mount_path) -> None:
    runner = FakeAgentRunner(responses=["output"])

    asyncio.run(
        runner.run(name="planner", prompt_file=prompt_file, mount_path=mount_path)
    )

    assert len(runner.calls) == 1
    assert runner.calls[0]["name"] == "planner"
    assert runner.calls[0]["prompt_file"] == prompt_file
    assert runner.calls[0]["mount_path"] == mount_path


def test_fake_agent_runner_records_all_calls(prompt_file, mount_path) -> None:
    runner = FakeAgentRunner(responses=["out1", "out2"])

    async def _run():
        await runner.run(name="planner", prompt_file=prompt_file, mount_path=mount_path)
        await runner.run(
            name="implementer", prompt_file=prompt_file, mount_path=mount_path
        )

    asyncio.run(_run())

    assert [c["name"] for c in runner.calls] == ["planner", "implementer"]


def test_fake_agent_runner_records_call_even_on_queue_exhaustion(
    prompt_file, mount_path
) -> None:
    runner = FakeAgentRunner(responses=[])

    with pytest.raises(AssertionError):
        asyncio.run(
            runner.run(name="planner", prompt_file=prompt_file, mount_path=mount_path)
        )

    assert len(runner.calls) == 1


def test_fake_agent_runner_queue_exhaustion_raises(prompt_file, mount_path) -> None:
    runner = FakeAgentRunner(responses=["only one"])

    async def _run():
        await runner.run(name="planner", prompt_file=prompt_file, mount_path=mount_path)
        await runner.run(
            name="implementer", prompt_file=prompt_file, mount_path=mount_path
        )

    with pytest.raises(AssertionError, match="queue exhausted"):
        asyncio.run(_run())


def test_fake_agent_runner_queue_exhaustion_error_names_agent(
    prompt_file, mount_path
) -> None:
    runner = FakeAgentRunner(responses=[])

    with pytest.raises(AssertionError, match="unexpected-agent"):
        asyncio.run(
            runner.run(
                name="unexpected-agent", prompt_file=prompt_file, mount_path=mount_path
            )
        )


def test_fake_agent_runner_can_return_preflight_failure(
    prompt_file, mount_path
) -> None:
    failure = PreflightFailure(failures=(("ruff", "ruff check .", "E501"),))
    runner = FakeAgentRunner(responses=[failure])

    result = asyncio.run(
        runner.run(name="planner", prompt_file=prompt_file, mount_path=mount_path)
    )

    assert result is failure
