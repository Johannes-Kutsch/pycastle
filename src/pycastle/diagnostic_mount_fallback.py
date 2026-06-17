from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias

from .managed_worktree_mount_policy import (
    ManagedWorktreeMountAccepted,
    ManagedWorktreeMountRejected,
    decide_managed_worktree_mount,
    should_reject_managed_worktree_mount,
)

_DIAGNOSTIC_MOUNT_FALLBACK_LABELS = ["bug", "needs-triage"]


class DiagnosticMountFallbackIssueTracker(Protocol):
    repo: str

    def search_open_issues_by_title(self, prefix: str) -> list[int]: ...

    def create_issue_in(
        self,
        owner_repo: str,
        title: str,
        body: str,
        labels: list[str],
    ) -> int: ...


@dataclass(frozen=True)
class DiagnosticMountFallbackIssue:
    issue_number: int
    title: str


DiagnosticMountDispatchDecision: TypeAlias = (
    ManagedWorktreeMountAccepted | DiagnosticMountFallbackIssue
)


def decide_diagnostic_mount_dispatch(
    *,
    repo_root,
    mount_path,
    caller: str,
    diagnostic_role: str,
    role_name: str,
    original_failure_summary: str,
    github_svc: DiagnosticMountFallbackIssueTracker,
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
    existing = github_svc.search_open_issues_by_title(title)
    if not isinstance(existing, list):
        existing = []
    if existing:
        return DiagnosticMountFallbackIssue(issue_number=existing[0], title=title)

    body = _build_fallback_issue_body(
        caller=caller,
        diagnostic_role=diagnostic_role,
        role_name=role_name,
        original_failure_summary=original_failure_summary,
        rejection=decision,
    )
    issue_number = github_svc.create_issue_in(
        github_svc.repo,
        title,
        body,
        _DIAGNOSTIC_MOUNT_FALLBACK_LABELS,
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
