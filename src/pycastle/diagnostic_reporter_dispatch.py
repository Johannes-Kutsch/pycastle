"""Diagnostic-reporter dispatch pipeline for pycastle.

This module owns the **diagnostic-reporter dispatch** term: the single deep seam
that composes the managed-worktree mount decision, an optional pre-run hook,
reporter-agent invocation, IssueOutput narrowing, and diagnostic-issue-report
validation into one async entry point.

No behavior is invented here; the module composes already-tested helpers:
``decide_diagnostic_mount_dispatch``, ``validate_diagnostic_issue_report``,
and the injected agent runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pycastle.agents.runner import AgentRunnerProtocol
    from pycastle.config import Config, StageOverride
    from pycastle.display.status_display import StatusDisplay
    from pycastle.prompts.dispatch import PromptInvocation
    from pycastle.services import GithubService

from pycastle.agents.output_protocol import AgentRole, IssueOutput
from pycastle.agents.runner import RunRequest
from pycastle.diagnostic_issue_report_validation import (
    DiagnosticIssueReportValidationAFK,
    DiagnosticIssueReportValidationHITL,
    validate_diagnostic_issue_report,
)
from pycastle.diagnostic_mount_fallback import (
    DiagnosticMountFallbackIssue,
    decide_diagnostic_mount_dispatch,
)


@dataclass(frozen=True)
class DiagnosticReporterDispatchMountFallback:
    issue_number: int


@dataclass(frozen=True)
class DiagnosticReporterDispatchHITL:
    issue_number: int


@dataclass(frozen=True)
class DiagnosticReporterDispatchAFK:
    issue_number: int


@dataclass(frozen=True)
class DiagnosticReporterDispatchValidationSkipped:
    issue_number: int


type DiagnosticReporterDispatchOutcome = (
    DiagnosticReporterDispatchMountFallback
    | DiagnosticReporterDispatchHITL
    | DiagnosticReporterDispatchAFK
    | DiagnosticReporterDispatchValidationSkipped
)


class _DiagnosticReporterDeps(Protocol):
    repo_root: Path
    agent_runner: AgentRunnerProtocol
    github_svc: GithubService
    cfg: Config
    status_display: StatusDisplay


async def run_diagnostic_reporter_dispatch(
    *,
    caller: str,
    diagnostic_role: str,
    role_name: str,
    original_failure_summary: str,
    prompt_invocation: PromptInvocation,
    stage_override: StageOverride,
    mount_path: Path,
    deps: _DiagnosticReporterDeps,
    pre_run_hook: Callable[[Path, PromptInvocation], None] | None = None,
    skip_validation: bool = False,
) -> DiagnosticReporterDispatchOutcome:
    """Run the diagnostic-reporter dispatch pipeline.

    Steps:
    1. Managed-worktree mount decision via ``decide_diagnostic_mount_dispatch``;
       hard-reject short-circuits with a ``DiagnosticReporterDispatchMountFallback``.
    2. Optional pre-run hook (side effects, scope-args mutation).
    3. Reporter-agent invocation through ``deps.agent_runner``.
    4. Narrowing of the result to ``IssueOutput``; raises ``RuntimeError`` otherwise.
    5. Validation via ``validate_diagnostic_issue_report``, unless
       ``skip_validation=True``, in which case the raw issue number is returned.
    """
    mount_decision = decide_diagnostic_mount_dispatch(
        repo_root=deps.repo_root,
        mount_path=mount_path,
        caller=caller,
        diagnostic_role=diagnostic_role,
        role_name=role_name,
        original_failure_summary=original_failure_summary,
        github_svc=deps.github_svc,
    )
    if isinstance(mount_decision, DiagnosticMountFallbackIssue):
        return DiagnosticReporterDispatchMountFallback(
            issue_number=mount_decision.issue_number
        )

    if pre_run_hook is not None:
        pre_run_hook(mount_path, prompt_invocation)

    result = await deps.agent_runner.run(
        RunRequest(
            name=caller,
            prompt=prompt_invocation,
            mount_path=mount_path,
            role=AgentRole(diagnostic_role),
            model=stage_override.model,
            effort=stage_override.effort,
            service=stage_override.service,
            status_display=deps.status_display,
        )
    )
    if not isinstance(result, IssueOutput):
        raise RuntimeError(
            f"{caller} returned unexpected output type: {type(result).__name__}"
        )

    if skip_validation:
        return DiagnosticReporterDispatchValidationSkipped(issue_number=result.number)

    validation = validate_diagnostic_issue_report(
        caller=caller,
        issue_output=result,
        cfg=deps.cfg,
        filed_issue_reader=deps.github_svc,
    )
    if isinstance(validation, DiagnosticIssueReportValidationHITL):
        return DiagnosticReporterDispatchHITL(issue_number=validation.issue_number)
    if not isinstance(validation, DiagnosticIssueReportValidationAFK):
        raise TypeError(
            "exhaustive: only HITL or AFK remain after isinstance check above"
        )
    return DiagnosticReporterDispatchAFK(issue_number=validation.issue_number)


__all__ = [
    "DiagnosticReporterDispatchAFK",
    "DiagnosticReporterDispatchHITL",
    "DiagnosticReporterDispatchMountFallback",
    "DiagnosticReporterDispatchOutcome",
    "DiagnosticReporterDispatchValidationSkipped",
    "run_diagnostic_reporter_dispatch",
]
