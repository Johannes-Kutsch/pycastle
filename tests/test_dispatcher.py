"""Tests for dispatcher.should_dispatch_improve: improve-mode gate."""

from pycastle.iteration.dispatcher import should_dispatch_improve


# ── No improve mode ──────────────────────────────────────────────────────────


def test_no_improve_mode_returns_false():
    """improve_mode=None always returns False."""
    assert not should_dispatch_improve(
        improve_mode=None,
        slept_once=False,
    )


def test_no_improve_mode_ignores_slept_once():
    """improve_mode=None returns False regardless of slept_once."""
    assert not should_dispatch_improve(
        improve_mode=None,
        slept_once=True,
    )


# ── endless mode ─────────────────────────────────────────────────────────────


def test_endless_dispatches_improve_when_idle():
    """endless + not slept → True."""
    assert should_dispatch_improve(
        improve_mode="endless",
        slept_once=False,
    )


def test_endless_dispatches_even_after_sleep():
    """endless + slept_once=True → True (slept_once ignored in endless)."""
    assert should_dispatch_improve(
        improve_mode="endless",
        slept_once=True,
    )


# ── until_sleep mode ─────────────────────────────────────────────────────────


def test_until_sleep_dispatches_before_first_sleep():
    """until_sleep + not slept yet → True."""
    assert should_dispatch_improve(
        improve_mode="until_sleep",
        slept_once=False,
    )


def test_until_sleep_returns_false_after_sleep():
    """until_sleep + slept_once=True → False (backlog cleared post-sleep)."""
    assert not should_dispatch_improve(
        improve_mode="until_sleep",
        slept_once=True,
    )
