"""Tests for dispatcher.should_dispatch_improve: improve-mode gate."""

import pytest

from pycastle.iteration.dispatcher import should_dispatch_improve

# N used as the cap value in parametrised cases
_N = 3


@pytest.mark.parametrize(
    "improve_mode, slept_once, dispatched_count, improve_max, expected",
    [
        # ── improve_mode=None: always False regardless of other args ──────────
        (None, False, 0, None, False),
        (None, False, 0, _N, False),
        (None, True, 0, None, False),
        (None, True, _N - 1, _N, False),
        (None, False, _N, _N, False),
        # ── endless mode, improve_max=None: always True ───────────────────────
        ("endless", False, 0, None, True),
        ("endless", True, 0, None, True),
        ("endless", False, _N - 1, None, True),
        # ── endless mode, improve_max=N: gated by count ───────────────────────
        ("endless", False, 0, _N, True),
        ("endless", False, _N - 1, _N, True),
        ("endless", False, _N, _N, False),
        ("endless", True, _N, _N, False),
        # ── until_sleep mode, improve_max=None ───────────────────────────────
        ("until_sleep", False, 0, None, True),
        ("until_sleep", False, _N - 1, None, True),
        ("until_sleep", True, 0, None, False),
        ("until_sleep", True, _N - 1, None, False),
        # ── until_sleep mode, improve_max=N: both exit conditions apply ───────
        ("until_sleep", False, 0, _N, True),
        ("until_sleep", False, _N - 1, _N, True),
        ("until_sleep", False, _N, _N, False),  # cap stops it
        ("until_sleep", True, 0, _N, False),  # sleep stops it
        ("until_sleep", True, _N - 1, _N, False),  # sleep stops it
        ("until_sleep", True, _N, _N, False),  # both stop it
    ],
)
def test_should_dispatch_improve_matrix(
    improve_mode, slept_once, dispatched_count, improve_max, expected
):
    result = should_dispatch_improve(
        improve_mode=improve_mode,
        slept_once=slept_once,
        dispatched_count=dispatched_count,
        improve_max=improve_max,
    )
    assert result is expected
