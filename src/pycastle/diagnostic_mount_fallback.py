from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from pycastle.services import GithubService

from pycastle.managed_worktree_mount_policy import (
    ManagedWorktreeMountAccepted,
    ManagedWorktreeMountRejected,
    decide_managed_worktree_mount,
    should_reject_managed_worktree_mount,
)
from pycastle.services.github_service import GithubServiceError
from pycastle.upstream_issue_filing import file_deduped_upstream_issue

_DIAGNOSTIC_MOUNT_FALLBACK_LABELS = ["bug", "needs-triage"]


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
    body = _build_fallback_issue_body(
        caller=caller,
        diagnostic_role=diagnostic_role,
        role_name=role_name,
        original_failure_summary=original_failure_summary,
        rejection=decision,
    )
    issue_number = file_deduped_upstream_issue(
        dedupe_query=title,
        title=title,
        body=body,
        labels=_DIAGNOSTIC_MOUNT_FALLBACK_LABELS,
        github_svc=github_svc,
    )
    if issue_number is None:
        raise GithubServiceError(
            "create_issue_in failed in decide_diagnostic_mount_dispatch"
        )
    return DiagnosticMountFallbackIssue(issue_number=issue_number, title=title)


def _build_fallback_issue_body(
    *,
    caller: str,
    diagnostic_role: str,
    role_name: str,
    original_failure_summary: str,
    rejection: ManagedWorktreeMountRejected,
) -> str:
    return (
        "## Diagnostic fallback\n\n"
        "No diagnostic agent ran.\n\n"
        f"Pycastle skipped `{caller}` because the managed worktree mount "
        "preconditions were invalid before provider setup.\n\n"
        f"- Role: {role_name}\n"
        f"- Diagnostic role: {diagnostic_role}\n"
        f"- Expected mount path: {rejection.expected_mount_path}\n"
        f"- Provided mount path: {rejection.mount_path}\n"
        f"- Expected worktrees dir: {rejection.expected_worktrees_dir}\n"
        f"- Reason: {rejection.rejection_code}\n"
        f"- Rejection detail: {rejection.detail}\n\n"
        "## Original failure summary\n\n"
        f"{original_failure_summary}\n"
    )
