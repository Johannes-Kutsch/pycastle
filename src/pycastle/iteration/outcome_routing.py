from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import TypeAlias

from ..bug_reporter import (
    BUG_REPORT_LABEL_LIST,
    auto_file_issue,
    file_operator_actionable_git_issue,
)
from ..config import Config
from ..display.status_display import StatusDisplay
from ..services import GithubService, ServiceRegistry
from . import (
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
from .usage_limit_decision import (
    ContinueNow,
    SleepUntil,
    Stop,
    decide_model_not_available_continuation,
    decide_usage_limit_continuation,
)


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


@dataclasses.dataclass(frozen=True)
class ExitFailure:
    code: int


LoopDirective: TypeAlias = ContinueLoop | SleepThenContinue | BreakLoop | ExitFailure


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
        case AbortedSetup(
            phase=phase,
            message=message,
            command=command,
            output=output,
        ):
            first_line = next(iter(message.splitlines()), "")
            title = f"[pycastle] {phase} setup failure: {first_line}"
            body_parts = [
                "## Setup phase failure\n",
                f"Phase: {phase}\n",
                f"```\n{message}\n```\n",
            ]
            if command:
                body_parts.append(f"Command: `{command}`\n")
            if output:
                body_parts.append(f"Output:\n\n```\n{output}\n```\n")
            body = "\n".join(body_parts)
            url = auto_file_issue(title, body, BUG_REPORT_LABEL_LIST, cfg=deps.cfg)
            local_parts = [f"{phase} setup failed: {message}"]
            if command:
                local_parts.append(f"Command: {command}")
            if output:
                local_parts.append(f"Output: {output}")
            deps.status_display.print(
                "",
                "\n".join(local_parts) + (f"\nReport: {url}" if url else ""),
            )
            return ExitFailure(code=1)
        case Continue():
            return ContinueLoop()
