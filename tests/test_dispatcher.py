"""Tests for dispatcher: decide_iteration_action (work routing) and should_dispatch_improve."""

from pycastle.iteration.dispatcher import (
    Done,
    RunImplementDirect,
    RunPlan,
    decide_iteration_action,
    should_dispatch_improve,
)


# ── decide_iteration_action: in-flight takes priority ────────────────────────


def test_in_flight_issues_route_to_implement_direct():
    """When in_flight_count > 0, always route to RunImplementDirect regardless of afk count."""
    action = decide_iteration_action(open_afk_count=0, in_flight_count=1)
    assert isinstance(action, RunImplementDirect)


def test_in_flight_with_open_afk_still_routes_direct():
    """In-flight issues take priority over open AFK issues — no planning when in-flight."""
    action = decide_iteration_action(open_afk_count=5, in_flight_count=2)
    assert isinstance(action, RunImplementDirect)


# ── decide_iteration_action: open AFK issues ─────────────────────────────────


def test_two_or_more_open_afk_routes_to_plan():
    """Two or more open AFK issues with no in-flight → RunPlan."""
    action = decide_iteration_action(open_afk_count=2, in_flight_count=0)
    assert isinstance(action, RunPlan)


def test_many_open_afk_routes_to_plan():
    """Many open AFK issues → RunPlan."""
    action = decide_iteration_action(open_afk_count=10, in_flight_count=0)
    assert isinstance(action, RunPlan)


def test_single_open_afk_routes_to_plan():
    """Single open AFK issue → RunPlan (routes through planning, same as multi-issue)."""
    action = decide_iteration_action(open_afk_count=1, in_flight_count=0)
    assert isinstance(action, RunPlan)


# ── decide_iteration_action: no work ─────────────────────────────────────────


def test_no_work_returns_done():
    """Zero AFK + zero in-flight → Done."""
    action = decide_iteration_action(open_afk_count=0, in_flight_count=0)
    assert isinstance(action, Done)


# ── should_dispatch_improve: no improve mode ─────────────────────────────────


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


# ── should_dispatch_improve: already dispatched guard ────────────────────────


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


# ── should_dispatch_improve: endless mode ────────────────────────────────────


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


# ── should_dispatch_improve: until_sleep mode ────────────────────────────────


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
