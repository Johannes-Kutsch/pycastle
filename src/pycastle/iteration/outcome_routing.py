from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from pycastle.bug_reporter import auto_file_issue, file_operator_actionable_git_issue
from pycastle.iteration import (
    AbortedAgentCredentialFailure,
    AbortedAgentFailure,
    AbortedHardApiError,
    AbortedHITL,
    AbortedModelNotAvailable,
    AbortedOperatorActionable,
    AbortedSetup,
    AbortedTimeout,
    AbortedUsageLimit,
    Continue,
    Done,
    IterationOutcome,
    MergeCloseFailure,
    NoCandidate,
)
from pycastle.iteration.aborted_setup_report import (
    ExitFailure,
    translate_aborted_setup_to_directive,
)
from pycastle.iteration.usage_limit_decision import (
    ContinueNow,
    SleepUntil,
    Stop,
    decide_model_not_available_continuation,
    decide_usage_limit_continuation,
)

if TYPE_CHECKING:
    from datetime import datetime

    from pycastle.config import Config
    from pycastle.display.status_display import StatusDisplay
    from pycastle.services import GithubService, ServiceRegistry


@dataclasses.dataclass(frozen=True)
class ContinueLoop:
    pass


@dataclasses.dataclass(frozen=True)
class SleepThenContinue:
    wake_time: datetime
    message: str
    slept_once_after: bool = True


@dataclasses.dataclass(frozen=True)
class BreakLoop:
    pass


type LoopDirective = ContinueLoop | SleepThenContinue | BreakLoop | ExitFailure


@dataclasses.dataclass(frozen=True)
class RouterDeps:
    cfg: Config
    service_registry: ServiceRegistry | None
    now: datetime
    status_display: StatusDisplay
    github_svc: GithubService


def _continuation_to_directive(
    decision: ContinueNow | SleepUntil | Stop, deps: RouterDeps
) -> LoopDirective:
    if isinstance(decision, ContinueNow):
        if decision.message is not None:
            deps.status_display.print("", decision.message)
        return ContinueLoop()
    if isinstance(decision, SleepUntil):
        return SleepThenContinue(
            wake_time=decision.wake_time,
            message=decision.message,
            slept_once_after=True,
        )
    if decision.message is not None:
        deps.status_display.print("", decision.message)
    return BreakLoop()


def _route_usage_limit(outcome: AbortedUsageLimit, deps: RouterDeps) -> LoopDirective:
    return _continuation_to_directive(
        decide_usage_limit_continuation(
            outcome, deps.cfg, deps.service_registry, deps.now
        ),
        deps,
    )


def _route_model_not_available(
    outcome: AbortedModelNotAvailable, deps: RouterDeps
) -> LoopDirective:
    return _continuation_to_directive(
        decide_model_not_available_continuation(
            outcome, deps.cfg, deps.service_registry, deps.now
        ),
        deps,
    )


def _route_agent_failure(
    outcome: AbortedAgentFailure, deps: RouterDeps
) -> LoopDirective:
    msg = f"Agent '{outcome.failed_role}' failed irrecoverably."
    if outcome.issue_number is not None:
        msg += f" Filed issue #{outcome.issue_number} for triage."
    deps.status_display.print("", msg)
    return ExitFailure(code=1)


def _route_operator_actionable(
    outcome: AbortedOperatorActionable, deps: RouterDeps
) -> LoopDirective:
    deps.status_display.print(
        "",
        f"git {outcome.op} failed after {outcome.attempt_count} attempt(s) — remote unreachable. "
        "Check SSH/network and retry.",
    )
    file_operator_actionable_git_issue(
        op=outcome.op,
        stderr=outcome.stderr,
        attempt_count=outcome.attempt_count,
        github_svc=deps.github_svc,
    )
    return ExitFailure(code=1)


def _route_merge_close_failure(
    outcome: MergeCloseFailure, deps: RouterDeps
) -> LoopDirective:
    numbers_str = ", ".join(f"#{n}" for n in outcome.filed_issue_numbers)
    deps.status_display.print(
        "",
        f"Merge close failure: issue close failed. Filed {numbers_str} for triage.",
    )
    return BreakLoop()


def _route_timeout(outcome: AbortedTimeout, deps: RouterDeps) -> LoopDirective:
    deps.status_display.print(
        "",
        f"Agent '{outcome.failed_role}' timed out. Resuming next iteration.",
    )
    return ContinueLoop()


def _route_terminal(
    outcome: IterationOutcome, deps: RouterDeps
) -> LoopDirective | None:
    match outcome:
        case Done(improve_cap_reached=True):
            deps.status_display.print(
                "",
                f"improve_max ({deps.cfg.improve_max}) dispatches reached. Stopping.",
            )
            return BreakLoop()
        case Done():
            deps.status_display.print(
                "",
                (
                    f"No unblocked issues with label '{deps.cfg.issue_label}' "
                    "found. Skipping."
                ),
            )
            return BreakLoop()
        case NoCandidate():
            deps.status_display.print(
                "",
                "Improve agent reported no improvement candidate.",
            )
            return BreakLoop()
        case Continue():
            return ContinueLoop()
        case _:
            return None


def route_outcome(outcome: IterationOutcome, deps: RouterDeps) -> LoopDirective:
    terminal = _route_terminal(outcome, deps)
    if terminal is not None:
        return terminal
    match outcome:
        case AbortedHITL() | AbortedAgentCredentialFailure() | AbortedHardApiError():
            return ExitFailure(code=1)
        case AbortedTimeout():
            return _route_timeout(outcome, deps)
        case AbortedUsageLimit():
            return _route_usage_limit(outcome, deps)
        case AbortedModelNotAvailable():
            return _route_model_not_available(outcome, deps)
        case AbortedAgentFailure():
            return _route_agent_failure(outcome, deps)
        case AbortedOperatorActionable():
            return _route_operator_actionable(outcome, deps)
        case MergeCloseFailure():
            return _route_merge_close_failure(outcome, deps)
        case AbortedSetup():
            return translate_aborted_setup_to_directive(
                outcome, deps.cfg, deps.status_display, auto_file_issue
            )
        case _:
            raise TypeError(f"Unhandled outcome type: {type(outcome)}")
