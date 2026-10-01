"""Tests for iteration.iteration_dispatch — dispatch_iteration."""

from __future__ import annotations

from pycastle.iteration.iteration_dispatch import (
    RunImprove,
    RunPlanning,
    dispatch_iteration,
)


def test_all_empty_not_interrupted_returns_run_improve_no_pending_work() -> None:
    result = dispatch_iteration(
        open_issues=[], in_flight=[], improve_cycle_interrupted=False
    )
    assert result == RunImprove(had_pending_work=False)


def test_nonempty_open_issues_not_interrupted_returns_run_planning() -> None:
    result = dispatch_iteration(
        open_issues=["issue-1"], in_flight=[], improve_cycle_interrupted=False
    )
    assert result == RunPlanning()


def test_nonempty_in_flight_not_interrupted_returns_run_planning() -> None:
    result = dispatch_iteration(
        open_issues=[], in_flight=["branch-1"], improve_cycle_interrupted=False
    )
    assert result == RunPlanning()


def test_nonempty_open_issues_interrupted_returns_run_improve_with_pending_work() -> (
    None
):
    result = dispatch_iteration(
        open_issues=["issue-1"], in_flight=[], improve_cycle_interrupted=True
    )
    assert result == RunImprove(had_pending_work=True)


def test_nonempty_in_flight_interrupted_returns_run_improve_with_pending_work() -> None:
    result = dispatch_iteration(
        open_issues=[], in_flight=["branch-1"], improve_cycle_interrupted=True
    )
    assert result == RunImprove(had_pending_work=True)


def test_both_nonempty_not_interrupted_returns_run_planning() -> None:
    result = dispatch_iteration(
        open_issues=["issue-1"], in_flight=["branch-1"], improve_cycle_interrupted=False
    )
    assert result == RunPlanning()


def test_both_nonempty_interrupted_returns_run_improve_with_pending_work() -> None:
    result = dispatch_iteration(
        open_issues=["issue-1"], in_flight=["branch-1"], improve_cycle_interrupted=True
    )
    assert result == RunImprove(had_pending_work=True)


def test_all_empty_interrupted_returns_run_improve_no_pending_work() -> None:
    result = dispatch_iteration(
        open_issues=[], in_flight=[], improve_cycle_interrupted=True
    )
    assert result == RunImprove(had_pending_work=False)
