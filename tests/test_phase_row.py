import asyncio

import pytest

from pycastle.iteration._phase_row import phase_row
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


def test_phase_row_finally_does_nothing_if_close_was_called(
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = PlainStatusDisplay()

    async def run() -> None:
        async with phase_row(d, "MyPhase") as row:
            row.close("done")

    asyncio.run(run())
    out = capsys.readouterr().out
    assert "[MyPhase] failed" not in out
    assert out.count("[MyPhase]") == 2  # only started + done
