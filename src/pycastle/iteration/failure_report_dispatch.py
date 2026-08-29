"""Failure-Report agent-dispatch pipeline for the pycastle iteration loop.

This module owns the AgentFailedError abort pipeline: diagnostic-mount gate,
invocation-log copy into the Failure-Report evidence area (ADR 0035),
Failure-Report RunRequest construction, agent-runner await, IssueOutput extraction,
credential-failure delegation, and final AbortedAgentFailure construction.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from agent_runtime.errors import AgentCredentialFailureError, HardAgentError

from pycastle.agents.output_protocol import AgentRole, IssueOutput
from pycastle.agents.runner import RunRequest
from pycastle.diagnostic_mount_fallback import (
    DiagnosticMountFallbackIssue,
    decide_diagnostic_mount_dispatch,
)
from pycastle.errors import (
    AgentTimeoutError,
    ModelNotAvailableError,
    SetupPhaseError,
    TransientAgentError,
    UsageLimitError,
    WorktreeError,
    WorktreeTimeoutError,
)
from pycastle.prompts.dispatch import build_prompt_invocation
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.prompts.scope_args import build_failure_report_scope_args
from pycastle.services import GithubServiceError

if TYPE_CHECKING:
    from pycastle.errors import AgentFailedError
    from pycastle.iteration import AbortedAgentCredentialFailure, AbortedAgentFailure
    from pycastle.iteration._deps import Deps

_EVIDENCE_DIR = Path(".pycastle-session") / "failure-report"
_EVIDENCE_FILENAME = "agent-invocation.log"


def _evidence_relative_path() -> str:
    return (_EVIDENCE_DIR / _EVIDENCE_FILENAME).as_posix()


def _copy_invocation_log_to_evidence_area(
    *,
    worktree_path: Path,
    source: Path | str | None,
) -> Path | None:
    if source is None:
        return None
    if not worktree_path.is_dir():
        return None
    source_path = Path(source)
    if not source_path.is_file():
        return None

    destination = worktree_path / _EVIDENCE_DIR / _EVIDENCE_FILENAME
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)
    except OSError:
        return None
    else:
        return destination


async def translate_agent_failed_error_to_abort(
    err: AgentFailedError,
    deps: Deps,
) -> AbortedAgentFailure | AbortedAgentCredentialFailure:
    """Translate an AgentFailedError into AbortedAgentFailure or AbortedAgentCredentialFailure.

    Runs the Failure-Report agent-dispatch pipeline: diagnostic-mount gate,
    evidence copy (ADR 0035), RunRequest construction, agent await, and abort return.
    """
    from pycastle.iteration import (
        AbortedAgentFailure,
        _route_and_abort_agent_credential_failure,
    )

    issue_number: int | None = None
    if deps.cfg.diagnose_on_failure:
        try:
            mount_decision = decide_diagnostic_mount_dispatch(
                repo_root=deps.repo_root,
                mount_path=err.worktree_path,
                caller="Failure Report Agent",
                diagnostic_role=AgentRole.FAILURE_REPORT.value,
                role_name=err.role_value,
                original_failure_summary=(
                    f"Agent role {err.role_value!r} failed in worktree "
                    f"{err.worktree_path}."
                ),
                github_svc=deps.github_svc,
            )
            if isinstance(mount_decision, DiagnosticMountFallbackIssue):
                issue_number = mount_decision.issue_number
                return AbortedAgentFailure(
                    failed_role=err.role_value, issue_number=issue_number
                )
            raw_evidence_path = getattr(err, "agent_invocation_log_path", None)
            copied_evidence = _copy_invocation_log_to_evidence_area(
                worktree_path=err.worktree_path,
                source=raw_evidence_path,
            )
            if copied_evidence is not None:
                err.agent_invocation_log_path = _evidence_relative_path()
            else:
                err.agent_invocation_log_path = ""
            result = await deps.agent_runner.run(
                RunRequest(
                    name="Failure Report Agent",
                    prompt=build_prompt_invocation(
                        PromptTemplate.FAILURE_REPORT,
                        build_failure_report_scope_args(err),
                    ),
                    mount_path=err.worktree_path,
                    role=AgentRole.FAILURE_REPORT,
                    service=deps.cfg.preflight_issue_override.service,
                    status_display=deps.status_display,
                )
            )
            if isinstance(result, IssueOutput):
                issue_number = result.number
        except AgentCredentialFailureError as report_err:
            routed_result = _route_and_abort_agent_credential_failure(report_err, deps)
            if routed_result is None:
                raise RuntimeError(
                    "narrowing: credential failure always produces a route result"
                ) from None
            return routed_result
        except (
            AgentTimeoutError,
            TransientAgentError,
            HardAgentError,
            UsageLimitError,
            SetupPhaseError,
            WorktreeError,
            WorktreeTimeoutError,
            ModelNotAvailableError,
            GithubServiceError,
            OSError,
        ) as report_err:
            deps.status_display.print(
                "Failure Report",
                "Failure-Report agent crashed — no issue filed",
                "warning",
            )
            deps.logger.log_internal_error(
                f"Failure-Report agent crashed (original failure: role={err.role_value})",
                report_err,
                cause=err,
            )
    return AbortedAgentFailure(failed_role=err.role_value, issue_number=issue_number)
