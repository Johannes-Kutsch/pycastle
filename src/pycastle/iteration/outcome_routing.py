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


def route_outcome(outcome: IterationOutcome, deps: RouterDeps) -> LoopDirective:
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
        case AbortedHITL():
            return ExitFailure(code=1)
        case AbortedAgentCredentialFailure():
            return ExitFailure(code=1)
        case AbortedHardApiError():
            return ExitFailure(code=1)
        case AbortedUsageLimit():
            return _continuation_to_directive(
                decide_usage_limit_continuation(
                    outcome,
                    deps.cfg,
                    deps.service_registry,
                    deps.now,
                ),
                deps,
            )
        case AbortedModelNotAvailable():
            return _continuation_to_directive(
                decide_model_not_available_continuation(
                    outcome,
                    deps.cfg,
                    deps.service_registry,
                    deps.now,
                ),
                deps,
            )
        case AbortedAgentFailure(failed_role=role, issue_number=issue_num):
            msg = f"Agent '{role}' failed irrecoverably."
            if issue_num is not None:
                msg += f" Filed issue #{issue_num} for triage."
            deps.status_display.print("", msg)
            return ExitFailure(code=1)
        case AbortedTimeout(failed_role=role):
            deps.status_display.print(
                "",
                f"Agent '{role}' timed out. Resuming next iteration.",
            )
            return ContinueLoop()
        case AbortedOperatorActionable(op=op, stderr=stderr, attempt_count=cnt):
            deps.status_display.print(
                "",
                f"git {op} failed after {cnt} attempt(s) — remote unreachable. "
                "Check SSH/network and retry.",
            )
            file_operator_actionable_git_issue(
                op=op,
                stderr=stderr,
                attempt_count=cnt,
                github_svc=deps.github_svc,
            )
            return ExitFailure(code=1)
        case MergeCloseFailure(filed_issue_numbers=issue_numbers):
            numbers_str = ", ".join(f"#{n}" for n in issue_numbers)
            deps.status_display.print(
                "",
                f"Merge close failure: issue close failed. Filed {numbers_str} for triage.",
            )
            return BreakLoop()
        case AbortedSetup():
            return translate_aborted_setup_to_directive(
                outcome, deps.cfg, deps.status_display, auto_file_issue
            )
        case Continue():
            return ContinueLoop()
