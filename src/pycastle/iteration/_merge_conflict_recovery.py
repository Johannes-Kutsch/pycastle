import dataclasses
import hashlib
from pathlib import Path
from typing import Protocol

from agent_runtime.errors import HardAgentError

from pycastle.agents.output_protocol import (
    AgentOutputProtocolError,
    AgentRole,
    CommitMessageOutput,
)
from pycastle.agents.runner import AgentRunnerProtocol, RunRequest
from pycastle.bug_reporter import file_merge_close_failure_issue
from pycastle.config import Config
from pycastle.display.status_display import StatusDisplay
from pycastle.errors import (
    AgentTimeoutError,
    ModelNotAvailableError,
    SetupPhaseError,
    TransientAgentError,
    UsageLimitError,
    WorktreeError,
    WorktreeTimeoutError,
)
from pycastle.infrastructure.worktree import (
    replaceable_merge_sandbox_worktree,
    reusable_sandbox_worktree,
    teardown_worktree,
    worktree_identity,
)
from pycastle.iteration._fingerprint import prepare_fingerprint_gate
from pycastle.iteration._merge_reporting import MergeProgressReporter
from pycastle.iteration.implement import branch_for
from pycastle.managed_worktree_mount_policy import (
    ManagedWorktreeMountRejected,
    decide_managed_worktree_mount,
    describe_managed_worktree_mount_rejection,
    should_reject_managed_worktree_mount,
)
from pycastle.prompts.dispatch import build_prompt_invocation
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.prompts.scope_args import build_merge_scope_args
from pycastle.services import (
    GitCommandError,
    GithubService,
    GithubServiceError,
    GitService,
)
from pycastle.session import RoleSession

MERGE_SANDBOX_PREFIX = "pycastle/merge-sandbox"


class _ConflictRecoveryDeps(Protocol):
    git_svc: GitService
    github_svc: GithubService
    cfg: Config
    status_display: StatusDisplay
    agent_runner: AgentRunnerProtocol
    repo_root: Path


@dataclasses.dataclass(frozen=True)
class ConflictRecoveryOutcome:
    completed_conflicts: list[dict]
    pending_conflicts: list[dict]
    deleted_conflict_branches: list[str]
    close_failure_issue_numbers: list[int] = dataclasses.field(default_factory=list)

    @property
    def has_completed_conflicts(self) -> bool:
        return bool(self.completed_conflicts)

    @property
    def has_pending_conflicts(self) -> bool:
        return bool(self.pending_conflicts)

    def close_message_kwargs(self) -> dict[str, list[dict]]:
        return {
            "completed_conflicts": self.completed_conflicts,
            "pending_conflicts": self.pending_conflicts,
        }

    def merge_result_kwargs(self) -> dict[str, list[dict]]:
        return {
            "completed_conflicts": self.completed_conflicts,
            "pending_conflicts": self.pending_conflicts,
        }


def _ensure_conflict_branch_is_merged(
    issue: dict, path: Path, deps: _ConflictRecoveryDeps
) -> None:
    branch = branch_for(issue["number"])
    if deps.git_svc.is_ancestor(branch, path):
        return
    raise RuntimeError(f"{branch} is not a merged branch")


def _merge_sandbox_branch(issue_number: int) -> str:
    return f"{MERGE_SANDBOX_PREFIX}-issue-{issue_number}"


async def _delete_conflict_branch(
    branch: str,
    progress: MergeProgressReporter,
    deps: _ConflictRecoveryDeps,
) -> str | None:
    if not deps.git_svc.is_ancestor(branch, deps.repo_root):
        return None
    registered_worktrees = deps.git_svc.list_worktrees(deps.repo_root)
    worktree_path = worktree_identity(branch, deps.repo_root).path
    progress.update_remove_done(0)
    try:
        if worktree_path in registered_worktrees:
            try:
                teardown_worktree(deps.git_svc, deps.repo_root, worktree_path)
            except (GitCommandError, OSError) as exc:
                deps.status_display.print(
                    "Merge",
                    f"Warning: could not remove worktree for {branch!r}: {exc}",
                    "warning",
                )
        deps.git_svc.delete_branch(branch, deps.repo_root)
    except GitCommandError as exc:
        deps.status_display.print(
            "Merge",
            f"Warning: could not delete branch {branch!r}: {exc}",
            "warning",
        )
        return None
    finally:
        progress.update_remove_done(1)
        progress.update_remove_done(None)
    return branch


def _close_conflict_issue(issue: dict, deps: _ConflictRecoveryDeps) -> int | None:
    try:
        deps.github_svc.close_issue_with_parents(issue["number"])
    except GithubServiceError as exc:
        return file_merge_close_failure_issue(
            issue_number=issue["number"],
            exc=exc,
            github_svc=deps.github_svc,
        )
    return None


async def _recover_active_conflict(
    *,
    active_issue: dict,
    deps: _ConflictRecoveryDeps,
) -> Exception | None:
    sandbox_identity = worktree_identity(
        _merge_sandbox_branch(active_issue["number"]),
        deps.repo_root,
    )
    target_branch = deps.git_svc.get_current_branch(deps.repo_root)
    safe_sha = deps.git_svc.get_head_sha(deps.repo_root)
    conflict_branch = branch_for(active_issue["number"])
    fingerprint = hashlib.sha256((safe_sha + conflict_branch).encode()).hexdigest()
    role_session = RoleSession(sandbox_identity.path, AgentRole.MERGER)
    prepare_fingerprint_gate(role_session, fingerprint)
    if role_session.is_resumable():
        worktree_cm = reusable_sandbox_worktree(
            f"merge-sandbox-issue-{active_issue['number']}",
            sha=safe_sha,
            deps=deps,
        )
    else:
        worktree_cm = replaceable_merge_sandbox_worktree(
            issue_number=active_issue["number"],
            sha=safe_sha,
            deps=deps,
        )
    try:
        async with worktree_cm as sandbox_path:
            RoleSession(sandbox_path, AgentRole.MERGER).write_fingerprint(fingerprint)
            already_merged = deps.git_svc.start_merge(sandbox_path, conflict_branch)
            if already_merged:
                _ensure_conflict_branch_is_merged(active_issue, sandbox_path, deps)
                deps.git_svc.fast_forward_branch(
                    deps.repo_root, target_branch, sandbox_identity.branch
                )
                _ensure_conflict_branch_is_merged(active_issue, deps.repo_root, deps)
                RoleSession(sandbox_path, AgentRole.MERGER).discard()
                return None
            mount_decision = decide_managed_worktree_mount(
                repo_root=deps.repo_root,
                mount_path=sandbox_path,
                caller="Merge Agent",
                role=AgentRole.MERGER.value,
            )
            if isinstance(
                mount_decision, ManagedWorktreeMountRejected
            ) and should_reject_managed_worktree_mount(mount_decision):
                raise SetupPhaseError(
                    AgentRole.MERGER.value,
                    describe_managed_worktree_mount_rejection(mount_decision),
                )
            result = await deps.agent_runner.run(
                RunRequest(
                    name="Merge Agent",
                    prompt=build_prompt_invocation(
                        PromptTemplate.MERGE,
                        build_merge_scope_args(
                            active_issue=active_issue,
                        ),
                    ),
                    mount_path=sandbox_path,
                    role=AgentRole.MERGER,
                    model=deps.cfg.merge_override.model,
                    status_display=deps.status_display,
                    effort=deps.cfg.merge_override.effort,
                    service=deps.cfg.merge_override.service,
                    stage="pre-merge",
                    work_body=f"Merging branch {branch_for(active_issue['number'])}",
                )
            )
            if isinstance(result, CommitMessageOutput):
                deps.git_svc.commit(
                    sandbox_path,
                    deps.repo_root,
                    result.message or active_issue["title"],
                )
            _ensure_conflict_branch_is_merged(active_issue, sandbox_path, deps)
            deps.git_svc.fast_forward_branch(
                deps.repo_root, target_branch, sandbox_identity.branch
            )
            _ensure_conflict_branch_is_merged(active_issue, deps.repo_root, deps)
            RoleSession(sandbox_path, AgentRole.MERGER).discard()
    except (
        AgentTimeoutError,
        UsageLimitError,
        TransientAgentError,
        HardAgentError,
        WorktreeError,
        WorktreeTimeoutError,
    ):
        raise
    except (
        SetupPhaseError,
        RuntimeError,
        GitCommandError,
        ModelNotAvailableError,
        AgentOutputProtocolError,
    ) as exc:
        return exc
    return None


async def recover_conflicts(
    *,
    conflict_issues: list[dict],
    progress: MergeProgressReporter,
    deps: _ConflictRecoveryDeps,
) -> ConflictRecoveryOutcome:
    completed_conflicts: list[dict] = []
    deleted_conflict_branches: list[str] = []
    close_failure_issue_numbers: list[int] = []

    for idx, active_issue in enumerate(conflict_issues):
        error = await _recover_active_conflict(
            active_issue=active_issue,
            deps=deps,
        )
        if error is not None:
            deps.status_display.print(
                "Merge",
                f"Conflict branch {branch_for(active_issue['number'])} failed and remains pending: {error}",
                "warning",
            )
            return ConflictRecoveryOutcome(
                completed_conflicts=completed_conflicts,
                pending_conflicts=conflict_issues[idx:],
                deleted_conflict_branches=deleted_conflict_branches,
                close_failure_issue_numbers=close_failure_issue_numbers,
            )

        progress.update_merge_done(progress.merge_done + 1)
        deleted_branch = await _delete_conflict_branch(
            branch_for(active_issue["number"]), progress, deps
        )
        if deleted_branch is not None:
            deleted_conflict_branches.append(deleted_branch)
        filed_number = _close_conflict_issue(active_issue, deps)
        if filed_number is None:
            progress.update_close_done(progress.close_done + 1)
        else:
            close_failure_issue_numbers.append(filed_number)
        completed_conflicts.append(active_issue)

    return ConflictRecoveryOutcome(
        completed_conflicts=completed_conflicts,
        pending_conflicts=[],
        deleted_conflict_branches=deleted_conflict_branches,
        close_failure_issue_numbers=close_failure_issue_numbers,
    )
