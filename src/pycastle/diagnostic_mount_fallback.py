from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from pycastle.services import GithubService

from pycastle.managed_worktree_mount_policy import (
    ManagedWorktreeMountAccepted,
    decide_managed_worktree_mount,
    should_reject_managed_worktree_mount,
)
from pycastle.services.github_service import GithubServiceError
from pycastle.upstream_issue_report import (
    BUG_AND_TRIAGE_LABELS,
    UpstreamIssueReport,
    diagnostic_mount_fallback_body,
    file_upstream_issue,
)


@dataclass(frozen=True)
class DiagnosticMountFallbackIssue:
    issue_number: int
    title: str


type DiagnosticMountDispatchDecision = (
    ManagedWorktreeMountAccepted | DiagnosticMountFallbackIssue
)


def decide_diagnostic_mount_dispatch(
    *,
    repo_root: Path,
    mount_path: Path,
    caller: str,
    diagnostic_role: str,
    role_name: str,
    original_failure_summary: str,
    github_svc: GithubService,
) -> DiagnosticMountDispatchDecision:
    decision = decide_managed_worktree_mount(
        repo_root=repo_root,
        mount_path=mount_path,
        caller=caller,
        role=diagnostic_role,
    )
    if isinstance(decision, ManagedWorktreeMountAccepted):
        return decision
    if not should_reject_managed_worktree_mount(decision):
        return ManagedWorktreeMountAccepted(
            caller=decision.caller,
            role=decision.role,
            repo_root=decision.repo_root,
            mount_path=decision.mount_path,
            expected_worktrees_dir=decision.expected_worktrees_dir,
        )

    title = (
        f"[pycastle] {caller} skipped for role {role_name}: "
        f"managed mount {decision.rejection_code}"
    )
    body = diagnostic_mount_fallback_body(
        caller=caller,
        diagnostic_role=diagnostic_role,
        role_name=role_name,
        original_failure_summary=original_failure_summary,
        rejection=decision,
    )
    issue_number = file_upstream_issue(
        UpstreamIssueReport(
            dedupe_key=title,
            title=title,
            body=body,
            labels=BUG_AND_TRIAGE_LABELS,
            github_svc=github_svc,
        )
    )
    if issue_number is None:
        raise GithubServiceError(
            "create_issue_in failed in decide_diagnostic_mount_dispatch"
        )
    return DiagnosticMountFallbackIssue(issue_number=issue_number, title=title)
