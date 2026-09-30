from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from pycastle.bug_reporter import (
    BUG_REPORT_LABEL_LIST,
    file_operator_actionable_git_issue,
)
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

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from pycastle.config import Config
    from pycastle.display.status_display import StatusDisplay
    from pycastle.services import GithubService, ServiceRegistry


@dataclasses.dataclass(frozen=True)
class ExitFailure:
    code: int
    message: str | None = None


@dataclasses.dataclass(frozen=True)
class ContinueLoop:
    message: str | None = None


@dataclasses.dataclass(frozen=True)
class SleepThenContinue:
    wake_time: datetime
    message: str


@dataclasses.dataclass(frozen=True)
class BreakLoop:
    message: str | None = None


type LoopDirective = ContinueLoop | SleepThenContinue | BreakLoop | ExitFailure


@dataclasses.dataclass(frozen=True)
class RouterDeps:
    cfg: Config
    service_registry: ServiceRegistry | None
    now: datetime
    status_display: StatusDisplay
    github_svc: GithubService
    bug_filer: Callable[..., str | None]


def _route_agent_failure(
    outcome: AbortedAgentFailure, _deps: RouterDeps
) -> LoopDirective:
    msg = f"Agent '{outcome.failed_role}' failed irrecoverably."
    if outcome.issue_number is not None:
        msg += f" Filed issue #{outcome.issue_number} for triage."
    return ExitFailure(code=1, message=msg)


def _route_operator_actionable(
    outcome: AbortedOperatorActionable, deps: RouterDeps
) -> LoopDirective:
    msg = (
        f"git {outcome.op} failed after {outcome.attempt_count} attempt(s) — remote unreachable. "
        "Check SSH/network and retry."
    )
    file_operator_actionable_git_issue(
        op=outcome.op,
        stderr=outcome.stderr,
        attempt_count=outcome.attempt_count,
        github_svc=deps.github_svc,
    )
    return ExitFailure(code=1, message=msg)


def _route_merge_close_failure(
    outcome: MergeCloseFailure, _deps: RouterDeps
) -> LoopDirective:
    numbers_str = ", ".join(f"#{n}" for n in outcome.filed_issue_numbers)
    msg = f"Merge close failure: issue close failed. Filed {numbers_str} for triage."
    return BreakLoop(message=msg)


def _route_timeout(outcome: AbortedTimeout, _deps: RouterDeps) -> LoopDirective:
    msg = f"Agent '{outcome.failed_role}' timed out. Resuming next iteration."
    return ContinueLoop(message=msg)


def _route_terminal(
    outcome: IterationOutcome, deps: RouterDeps
) -> LoopDirective | None:
    match outcome:
        case Done(improve_cap_reached=True):
            return BreakLoop(
                message=f"improve_max ({deps.cfg.improve_max}) dispatches reached. Stopping."
            )
        case Done():
            return BreakLoop(
                message=(
                    f"No unblocked issues with label '{deps.cfg.issue_label}' "
                    "found. Skipping."
                )
            )
        case NoCandidate():
            return BreakLoop(message="Improve agent reported no improvement candidate.")
        case Continue():
            return ContinueLoop()
        case _:
            return None


def _route_aborted_setup(outcome: AbortedSetup, deps: RouterDeps) -> ExitFailure:
    from pycastle.upstream_issue_report import aborted_setup_body  # noqa: PLC0415

    phase = outcome.phase
    message = outcome.message
    command = outcome.command
    output = outcome.output

    first_line = next(iter(message.splitlines()), "")
    title = f"[pycastle] {phase} setup failure: {first_line}"
    body = aborted_setup_body(
        phase=phase, message=message, command=command, output=output
    )
    url = deps.bug_filer(title, body, BUG_REPORT_LABEL_LIST, cfg=deps.cfg)

    local_parts = [f"{phase} setup failed: {message}"]
    if command:
        local_parts.append(f"Command: {command}")
    if output:
        local_parts.append(f"Output: {output}")
    composed = "\n".join(local_parts) + (f"\nReport: {url}" if url else "")

    return ExitFailure(code=1, message=composed)


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
            from pycastle.iteration._usage_limit_helpers import (  # noqa: PLC0415
                decide_usage_limit_continuation,
            )

            return decide_usage_limit_continuation(
                outcome, deps.cfg, deps.service_registry, deps.now
            )
        case AbortedModelNotAvailable():
            from pycastle.iteration._usage_limit_helpers import (  # noqa: PLC0415
                decide_model_not_available_continuation,
            )

            return decide_model_not_available_continuation(
                outcome, deps.cfg, deps.service_registry, deps.now
            )
        case AbortedAgentFailure():
            return _route_agent_failure(outcome, deps)
        case AbortedOperatorActionable():
            return _route_operator_actionable(outcome, deps)
        case MergeCloseFailure():
            return _route_merge_close_failure(outcome, deps)
        case AbortedSetup():
            return _route_aborted_setup(outcome, deps)
        case _:
            raise TypeError(f"Unhandled outcome type: {type(outcome)}")
