import asyncio

import pytest

from pycastle.iteration import agent_row, phase_row
from pycastle.status_display import PlainStatusDisplay


def test_phase_row_success_path_register_and_close(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()

    async def run() -> None:
        async with phase_row(d, "MyPhase") as row:
            row.close("all done")

    asyncio.run(run())
    out = capsys.readouterr().out
    assert out == "\n[MyPhase] started\n[MyPhase] all done\n"


def test_phase_row_exception_path_auto_error_remove(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()

    async def run() -> None:
        async with phase_row(d, "MyPhase") as _row:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        asyncio.run(run())
    out = capsys.readouterr().out
    assert out == "\n[MyPhase] started\n[MyPhase] failed\n"


def test_phase_row_close_is_idempotent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()

    async def run() -> None:
        async with phase_row(d, "MyPhase") as row:
            row.close("done")
            row.close("done again")

    asyncio.run(run())
    out = capsys.readouterr().out
    assert out == "\n[MyPhase] started\n[MyPhase] done\n"


def test_phase_row_custom_startup_message_appears_in_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()

    async def run() -> None:
        async with phase_row(
            d,
            "Plan",
            startup_message="started planning for 3 issue(s) labeled ready-for-agent",
        ) as row:
            row.close("done")

    asyncio.run(run())
    out = capsys.readouterr().out
    assert (
        out
        == "\n[Plan] started planning for 3 issue(s) labeled ready-for-agent\n[Plan] done\n"
    )


def test_phase_row_custom_startup_message_appears_on_exception_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()

    async def run() -> None:
        async with phase_row(d, "Plan", startup_message="custom message") as _row:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        asyncio.run(run())
    out = capsys.readouterr().out
    assert out == "\n[Plan] custom message\n[Plan] failed\n"


def test_agent_row_success_path_registers_and_removes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()

    async def run() -> None:
        async with agent_row(d, "Worker", work_body="implementing #1") as ctx:
            assert ctx is None

    asyncio.run(run())
    out = capsys.readouterr().out
    assert out == "\n[Worker] started\n[Worker] finished\n"


def test_agent_row_exception_path_marks_failed_and_propagates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()

    async def run() -> None:
        async with agent_row(d, "Worker", work_body="implementing #1"):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(run())
    out = capsys.readouterr().out
    assert out == "\n[Worker] started\n[Worker] failed\n"


def test_agent_row_register_uses_agent_kind_and_work_body(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()

    async def run() -> None:
        async with phase_row(d, "Implement"):
            async with agent_row(d, "Worker", work_body="implementing #42"):
                pass

    asyncio.run(run())
    out = capsys.readouterr().out
    # No blank line between [Implement] started and [Worker] started, because
    # PlainStatusDisplay suppresses the blank between {phase, agent} kinds.
    # This is observable proof that agent_row registered with kind="agent".
    assert out == (
        "\n[Implement] started\n"
        "[Worker] started\n"
        "[Worker] finished\n"
        "[Implement] failed\n"
    )
