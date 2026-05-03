import asyncio

import pytest

from pycastle.iteration import phase_row
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
