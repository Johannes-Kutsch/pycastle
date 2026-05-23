import asyncio

import pytest

from pycastle.errors import AgentTimeoutError, UsageLimitError
from pycastle.iteration import StatusRow, status_row
from pycastle.display.status_display import PlainStatusDisplay


def test_phase_row_success_path_register_and_close(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()

    async def run() -> None:
        async with status_row(d, "MyPhase", kind="phase", must_close=True) as row:
            row.close("all done")

    asyncio.run(run())
    out = capsys.readouterr().out
    assert out == "\n[MyPhase] started\n[MyPhase] all done\n"


def test_phase_row_exception_path_auto_error_remove(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()

    async def run() -> None:
        async with status_row(d, "MyPhase", kind="phase", must_close=True) as _row:
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
        async with status_row(d, "MyPhase", kind="phase", must_close=True) as row:
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
        async with status_row(
            d,
            "Plan",
            kind="phase",
            must_close=True,
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
        async with status_row(
            d, "Plan", kind="phase", must_close=True, startup_message="custom message"
        ) as _row:
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
        async with status_row(
            d, "Worker", kind="agent", must_close=False, work_body="implementing #1"
        ) as row:
            assert isinstance(row, StatusRow)
            assert not row._closed

    asyncio.run(run())
    out = capsys.readouterr().out
    assert out == "\n[Worker] started\n[Worker] finished\n"


def test_agent_row_exception_path_marks_failed_and_propagates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()

    async def run() -> None:
        async with status_row(
            d, "Worker", kind="agent", must_close=False, work_body="implementing #1"
        ):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(run())
    out = capsys.readouterr().out
    assert out == "\n[Worker] started\n[Worker] failed\n"


def test_agent_row_usage_limit_paints_interrupted_and_propagates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()

    async def run() -> None:
        async with status_row(
            d, "Worker", kind="agent", must_close=False, work_body="implementing #1"
        ):
            raise UsageLimitError()

    with pytest.raises(UsageLimitError):
        asyncio.run(run())
    out = capsys.readouterr().out
    assert out == "\n[Worker] started\n[Worker] usage limit reached\n"


def test_agent_row_timeout_paints_interrupted_and_propagates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()

    async def run() -> None:
        async with status_row(
            d, "Worker", kind="agent", must_close=False, work_body="implementing #1"
        ):
            raise AgentTimeoutError("timed out")

    with pytest.raises(AgentTimeoutError):
        asyncio.run(run())
    out = capsys.readouterr().out
    assert out == "\n[Worker] started\n[Worker] timed out\n"


def test_agent_row_register_uses_agent_kind_and_work_body(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()

    async def run() -> None:
        async with status_row(d, "Implement", kind="phase", must_close=True):
            async with status_row(
                d,
                "Worker",
                kind="agent",
                must_close=False,
                work_body="implementing #42",
            ):
                pass

    asyncio.run(run())
    out = capsys.readouterr().out
    # No blank line between [Implement] started and [Worker] started, because
    # PlainStatusDisplay suppresses the blank between {phase, agent} kinds.
    # This is observable proof that agent kind was registered correctly.
    assert out == (
        "\n[Implement] started\n"
        "[Worker] started\n"
        "[Worker] finished\n"
        "[Implement] failed\n"
    )
