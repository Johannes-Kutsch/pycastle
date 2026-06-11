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
from ..infrastructure.worktree import managed_worktree, worktree_identity
from ..prompts.dispatch import build_prompt_invocation
from ..prompts.pipeline import PromptTemplate
from ..prompts.scope_args import build_merge_scope_args
from ..services import GitService
from ..session import RoleSession
from .implement import branch_for

MERGE_SANDBOX_PREFIX = "pycastle/merge-sandbox"


class _ConflictRecoveryDeps(Protocol):
    git_svc: GitService
    cfg: Config
    status_display: StatusDisplay
    agent_runner: AgentRunnerProtocol
    repo_root: Path


@dataclasses.dataclass(frozen=True)
class ConflictRecoveryCompleted:
    issue: dict


@dataclasses.dataclass(frozen=True)
class ConflictRecoveryPending:
    issues: list[dict]
    error: Exception


ConflictRecoveryOutcome = ConflictRecoveryCompleted | ConflictRecoveryPending


def _ensure_conflict_branch_is_merged(
    issue: dict, path: Path, deps: _ConflictRecoveryDeps
) -> None:
    branch = branch_for(issue["number"])
    if deps.git_svc.is_ancestor(branch, path):
        return
    raise RuntimeError(f"{branch} is not a merged branch")


def _merge_sandbox_branch(issue_number: int) -> str:
    return f"{MERGE_SANDBOX_PREFIX}-issue-{issue_number}"


async def recover_active_conflict(
    *,
    conflict_issues: list[dict],
    active_issue: dict,
    pending_issues: list[dict],
    deps: _ConflictRecoveryDeps,
) -> ConflictRecoveryOutcome:
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
        return ConflictRecoveryPending(issues=pending_issues, error=exc)
    return ConflictRecoveryCompleted(issue=active_issue)
