"""Typed iteration-dispatch helper that owns the ADR 0067 idle-gate rule.

ADR 0067 defines the idle gate: an interrupted improve cycle outranks planning
within a run. This module is the single authoritative site for that rule; any
future amendment to the gate logic should start here.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclasses.dataclass(frozen=True)
class RunPlanning:
    """Planning runs with the open issues already in hand."""


@dataclasses.dataclass(frozen=True)
class RunImprove:
    """Improve runs first; carries whether any pending work existed."""

    had_pending_work: bool


type IterationDispatch = RunPlanning | RunImprove


def dispatch_iteration(
    open_issues: Sequence[object],
    in_flight: Sequence[object],
    *,
    improve_cycle_interrupted: bool,
) -> IterationDispatch:
    """Apply the ADR 0067 idle-gate rule and return the appropriate dispatch variant.

    The gate is: ``(not open_issues and not in_flight) or improve_cycle_interrupted``.
    When the gate is true improve runs first; otherwise planning runs with the
    issues already in hand. The lists are consumed only for emptiness.
    """
    if (not open_issues and not in_flight) or improve_cycle_interrupted:
        return RunImprove(had_pending_work=bool(open_issues) or bool(in_flight))
    return RunPlanning()
