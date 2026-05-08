"""Tests for decide_iteration_action: iteration-entry routing dispatcher."""

from pycastle.iteration.dispatcher import (
    DispatchImprove,
    Done,
    RunImplementDirect,
    RunPlan,
    decide_iteration_action,
)


# ── Normal routing: in-flight takes priority ─────────────────────────────────


def test_in_flight_issues_route_to_implement_direct():
    """When in_flight_count > 0, always route to RunImplementDirect regardless of afk count."""
    action = decide_iteration_action(
        open_afk_count=0,
        in_flight_count=1,
        improve_mode=None,
        slept_once=False,
        improve_dispatched_this_iteration=False,
    )
    assert isinstance(action, RunImplementDirect)


def test_in_flight_with_open_afk_still_routes_direct():
    """In-flight issues take priority over open AFK issues — no planning when in-flight."""
    action = decide_iteration_action(
        open_afk_count=5,
        in_flight_count=2,
        improve_mode=None,
        slept_once=False,
        improve_dispatched_this_iteration=False,
    )
    assert isinstance(action, RunImplementDirect)


# ── Normal routing: open AFK issues ──────────────────────────────────────────


def test_two_or_more_open_afk_routes_to_plan():
    """Two or more open AFK issues with no in-flight → RunPlan."""
    action = decide_iteration_action(
        open_afk_count=2,
        in_flight_count=0,
        improve_mode=None,
        slept_once=False,
        improve_dispatched_this_iteration=False,
    )
    assert isinstance(action, RunPlan)


def test_many_open_afk_routes_to_plan():
    """Many open AFK issues → RunPlan."""
    action = decide_iteration_action(
        open_afk_count=10,
        in_flight_count=0,
        improve_mode=None,
        slept_once=False,
        improve_dispatched_this_iteration=False,
    )
    assert isinstance(action, RunPlan)


def test_single_open_afk_routes_to_implement_direct():
    """Single open AFK issue → RunImplementDirect (fast path, skip planning)."""
    action = decide_iteration_action(
        open_afk_count=1,
        in_flight_count=0,
        improve_mode=None,
        slept_once=False,
        improve_dispatched_this_iteration=False,
    )
    assert isinstance(action, RunImplementDirect)


# ── Idle: no work available ───────────────────────────────────────────────────


def test_no_work_no_improve_mode_returns_done():
    """Zero AFK + zero in-flight + no improve_mode → Done."""
    action = decide_iteration_action(
        open_afk_count=0,
        in_flight_count=0,
        improve_mode=None,
        slept_once=False,
        improve_dispatched_this_iteration=False,
    )
    assert isinstance(action, Done)


# ── Improve mode: endless ─────────────────────────────────────────────────────


def test_endless_mode_dispatches_improve_when_idle():
    """endless mode + zero work + not yet dispatched → DispatchImprove."""
    action = decide_iteration_action(
        open_afk_count=0,
        in_flight_count=0,
        improve_mode="endless",
        slept_once=False,
        improve_dispatched_this_iteration=False,
    )
    assert isinstance(action, DispatchImprove)


def test_endless_mode_returns_done_if_already_dispatched():
    """endless mode + already dispatched this iteration → Done (one-per-iteration guard)."""
    action = decide_iteration_action(
        open_afk_count=0,
        in_flight_count=0,
        improve_mode="endless",
        slept_once=False,
        improve_dispatched_this_iteration=True,
    )
    assert isinstance(action, Done)


def test_endless_mode_dispatches_even_after_sleep():
    """endless mode never stops due to slept_once — keeps generating."""
    action = decide_iteration_action(
        open_afk_count=0,
        in_flight_count=0,
        improve_mode="endless",
        slept_once=True,
        improve_dispatched_this_iteration=False,
    )
    assert isinstance(action, DispatchImprove)


# ── Improve mode: until_sleep ─────────────────────────────────────────────────


def test_until_sleep_dispatches_improve_before_first_sleep():
    """until_sleep mode + not slept yet + not dispatched → DispatchImprove."""
    action = decide_iteration_action(
        open_afk_count=0,
        in_flight_count=0,
        improve_mode="until_sleep",
        slept_once=False,
        improve_dispatched_this_iteration=False,
    )
    assert isinstance(action, DispatchImprove)


def test_until_sleep_returns_done_after_sleep_clears_backlog():
    """until_sleep mode + slept_once=True + no work → Done (backlog cleared post-sleep)."""
    action = decide_iteration_action(
        open_afk_count=0,
        in_flight_count=0,
        improve_mode="until_sleep",
        slept_once=True,
        improve_dispatched_this_iteration=False,
    )
    assert isinstance(action, Done)


def test_until_sleep_returns_done_if_already_dispatched():
    """until_sleep mode + already dispatched this iteration → Done."""
    action = decide_iteration_action(
        open_afk_count=0,
        in_flight_count=0,
        improve_mode="until_sleep",
        slept_once=False,
        improve_dispatched_this_iteration=True,
    )
    assert isinstance(action, Done)


def test_until_sleep_slept_and_dispatched_returns_done():
    """until_sleep mode + slept + dispatched → Done (slept takes priority)."""
    action = decide_iteration_action(
        open_afk_count=0,
        in_flight_count=0,
        improve_mode="until_sleep",
        slept_once=True,
        improve_dispatched_this_iteration=True,
    )
    assert isinstance(action, Done)


# ── improve_mode ignored when there is work ───────────────────────────────────


def test_improve_mode_ignored_when_afk_issues_present():
    """improve_mode is irrelevant when open_afk_count > 0 — normal routing takes precedence."""
    action = decide_iteration_action(
        open_afk_count=3,
        in_flight_count=0,
        improve_mode="endless",
        slept_once=False,
        improve_dispatched_this_iteration=False,
    )
    assert isinstance(action, RunPlan)


def test_improve_mode_ignored_when_in_flight_issues_present():
    """improve_mode is irrelevant when in_flight_count > 0."""
    action = decide_iteration_action(
        open_afk_count=0,
        in_flight_count=1,
        improve_mode="endless",
        slept_once=False,
        improve_dispatched_this_iteration=False,
    )
    assert isinstance(action, RunImplementDirect)


# ── Stop semantics: work takes priority over slept_once ───────────────────────


def test_until_sleep_work_takes_priority_over_slept_once_single_issue():
    """until_sleep + slept_once=True + one AFK issue → RunImplementDirect, not Done."""
    action = decide_iteration_action(
        open_afk_count=1,
        in_flight_count=0,
        improve_mode="until_sleep",
        slept_once=True,
        improve_dispatched_this_iteration=False,
    )
    assert isinstance(action, RunImplementDirect)


def test_until_sleep_work_takes_priority_over_slept_once_multiple_issues():
    """until_sleep + slept_once=True + multiple AFK issues → RunPlan, not Done."""
    action = decide_iteration_action(
        open_afk_count=2,
        in_flight_count=0,
        improve_mode="until_sleep",
        slept_once=True,
        improve_dispatched_this_iteration=False,
    )
    assert isinstance(action, RunPlan)


def test_until_sleep_in_flight_work_takes_priority_over_slept_once():
    """until_sleep + slept_once=True + in-flight issue → RunImplementDirect, not Done."""
    action = decide_iteration_action(
        open_afk_count=0,
        in_flight_count=1,
        improve_mode="until_sleep",
        slept_once=True,
        improve_dispatched_this_iteration=False,
    )
    assert isinstance(action, RunImplementDirect)


# ── Stop semantics: dispatched guard is mode-independent ─────────────────────


def test_endless_mode_dispatched_guard_fires_regardless_of_slept_once():
    """endless + slept_once=True + already dispatched → Done (one-per-iteration guard)."""
    action = decide_iteration_action(
        open_afk_count=0,
        in_flight_count=0,
        improve_mode="endless",
        slept_once=True,
        improve_dispatched_this_iteration=True,
    )
    assert isinstance(action, Done)


def test_until_sleep_dispatched_guard_fires_with_slept_once_false():
    """until_sleep + slept_once=False + already dispatched → Done (dispatched guard, not sleep gate)."""
    action = decide_iteration_action(
        open_afk_count=0,
        in_flight_count=0,
        improve_mode="until_sleep",
        slept_once=False,
        improve_dispatched_this_iteration=True,
    )
    assert isinstance(action, Done)
