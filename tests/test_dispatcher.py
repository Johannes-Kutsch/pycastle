"""Tests for dispatcher.should_dispatch_improve: improve-mode gate."""

from pycastle.iteration.dispatcher import should_dispatch_improve


# ── No improve mode ──────────────────────────────────────────────────────────


def test_no_improve_mode_returns_false():
    """improve_mode=None always returns False."""
    assert not should_dispatch_improve(
        improve_mode=None,
        slept_once=False,
        improve_dispatched_this_iteration=False,
    )


def test_no_improve_mode_ignores_slept_once():
    """improve_mode=None returns False regardless of slept_once."""
    assert not should_dispatch_improve(
        improve_mode=None,
        slept_once=True,
        improve_dispatched_this_iteration=False,
    )


# ── Already dispatched guard ─────────────────────────────────────────────────


def test_already_dispatched_returns_false_for_endless():
    """endless + already dispatched this iteration → False (one-per-iteration guard)."""
    assert not should_dispatch_improve(
        improve_mode="endless",
        slept_once=False,
        improve_dispatched_this_iteration=True,
    )


def test_already_dispatched_returns_false_for_until_sleep():
    """until_sleep + already dispatched this iteration → False."""
    assert not should_dispatch_improve(
        improve_mode="until_sleep",
        slept_once=False,
        improve_dispatched_this_iteration=True,
    )


def test_dispatched_guard_fires_regardless_of_slept_once():
    """endless + slept_once=True + already dispatched → False (guard is mode-independent)."""
    assert not should_dispatch_improve(
        improve_mode="endless",
        slept_once=True,
        improve_dispatched_this_iteration=True,
    )


# ── endless mode ─────────────────────────────────────────────────────────────


def test_endless_dispatches_improve_when_idle():
    """endless + not dispatched → True."""
    assert should_dispatch_improve(
        improve_mode="endless",
        slept_once=False,
        improve_dispatched_this_iteration=False,
    )


def test_endless_dispatches_even_after_sleep():
    """endless + slept_once=True + not dispatched → True (slept_once ignored in endless)."""
    assert should_dispatch_improve(
        improve_mode="endless",
        slept_once=True,
        improve_dispatched_this_iteration=False,
    )


# ── until_sleep mode ─────────────────────────────────────────────────────────


def test_until_sleep_dispatches_before_first_sleep():
    """until_sleep + not slept yet + not dispatched → True."""
    assert should_dispatch_improve(
        improve_mode="until_sleep",
        slept_once=False,
        improve_dispatched_this_iteration=False,
    )


def test_until_sleep_returns_false_after_sleep():
    """until_sleep + slept_once=True + no work → False (backlog cleared post-sleep)."""
    assert not should_dispatch_improve(
        improve_mode="until_sleep",
        slept_once=True,
        improve_dispatched_this_iteration=False,
    )


def test_until_sleep_slept_and_dispatched_returns_false():
    """until_sleep + slept + dispatched → False."""
    assert not should_dispatch_improve(
        improve_mode="until_sleep",
        slept_once=True,
        improve_dispatched_this_iteration=True,
    )
