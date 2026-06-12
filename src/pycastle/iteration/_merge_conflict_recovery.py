import dataclasses
from pathlib import Path
from typing import Protocol

from ..agents.output_protocol import AgentRole, CommitMessageOutput
from ..agents.runner import AgentRunnerProtocol, RunRequest
from ..config import Config
from ..display.status_display import StatusDisplay
from ..errors import (
    AgentTimeoutError,
    HardAgentError,
    TransientAgentError,
    UsageLimitError,
)
from ..infrastructure.worktree import (
    managed_worktree,
    teardown_worktree,
    worktree_identity,
)
from ..prompts.dispatch import build_prompt_invocation
from ..prompts.pipeline import PromptTemplate
from ..prompts.scope_args import build_merge_scope_args
from ..services import GitCommandError, GitService, GithubService
from ..session import RoleSession
from ._merge_reporting import MergeProgressReporter
from .implement import branch_for

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
            except Exception as exc:
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


def _close_conflict_issue(issue: dict, deps: _ConflictRecoveryDeps) -> None:
    try:
        deps.github_svc.close_issue(issue["number"])
    except Exception as exc:
        deps.status_display.print(
            "Merge",
            f"Warning: could not close issue #{issue['number']}: {exc}",
            "warning",
        )


async def _recover_active_conflict(
    *,
    conflict_issues: list[dict],
    active_issue: dict,
    deps: _ConflictRecoveryDeps,
) -> Exception | None:
    sandbox_identity = worktree_identity(
        _merge_sandbox_branch(active_issue["number"]),
        deps.repo_root,
    )
    target_branch = deps.git_svc.get_current_branch(deps.repo_root)
    try:
        async with managed_worktree(
            identity=sandbox_identity,
            sha=deps.git_svc.get_head_sha(deps.repo_root),
            delete_branch_on_teardown=True,
            replace_preserved_failure=True,
            deps=deps,
        ) as sandbox_path:
            deps.git_svc.start_merge(sandbox_path, branch_for(active_issue["number"]))
            result = await deps.agent_runner.run(
                RunRequest(
                    name="Merge Agent",
                    prompt=build_prompt_invocation(
                        PromptTemplate.MERGE,
                        build_merge_scope_args(
                            conflict_issues=conflict_issues,
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
    ):
        raise
    except Exception as exc:
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

    for idx, active_issue in enumerate(conflict_issues):
        error = await _recover_active_conflict(
            conflict_issues=conflict_issues,
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
            )

        progress.update_merge_done(progress.merge_done + 1)
        deleted_branch = await _delete_conflict_branch(
            branch_for(active_issue["number"]), progress, deps
        )
        if deleted_branch is not None:
            deleted_conflict_branches.append(deleted_branch)
        _close_conflict_issue(active_issue, deps)
        progress.update_close_done(progress.close_done + 1)
        completed_conflicts.append(active_issue)

    return ConflictRecoveryOutcome(
        completed_conflicts=completed_conflicts,
        pending_conflicts=[],
        deleted_conflict_branches=deleted_conflict_branches,
    )
