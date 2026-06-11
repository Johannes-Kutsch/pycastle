import asyncio
import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from ..agents.output_protocol import AgentRole, CommitMessageOutput
from ..agents.runner import AgentRunnerProtocol, RunRequest
from ..config import Config
from ..errors import (
    AgentTimeoutError,
    HardAgentError,
    TransientAgentError,
    UsageLimitError,
)
from ..prompts.dispatch import build_prompt_invocation
from ..prompts.pipeline import PromptTemplate
from ..prompts.scope_args import build_merge_scope_args
from ..services import GitCommandError, GitService, GithubService
from ..session import RoleSession
from ..display.status_display import StatusDisplay
from ..infrastructure.worktree import (
    managed_worktree,
    teardown_worktree,
    worktree_identity,
)
from ._merge_reporting import MergeProgressReporter, build_merge_close_message
from ._rows import status_row
from ._utils import _wait_for_clean_working_tree
from .implement import branch_for
from .preflight import PreflightAFK, PreflightCache, PreflightHITL


class _MergeDeps(Protocol):
    git_svc: GitService
    github_svc: GithubService
    cfg: Config
    status_display: StatusDisplay
    agent_runner: AgentRunnerProtocol
    repo_root: Path
    preflight_cache: PreflightCache


MERGE_SANDBOX_PREFIX = "pycastle/merge-sandbox"


@dataclasses.dataclass
class MergeResult:
    clean: list[dict]
    conflicts: list[dict]
    completed_conflicts: list[dict] = dataclasses.field(default_factory=list)
    pending_conflicts: list[dict] = dataclasses.field(default_factory=list)
    preflight_blocker: PreflightHITL | PreflightAFK | None = None


async def _delete_merged_branches(
    branches: list[str],
    deps: _MergeDeps,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[str]:
    total = len(branches)
    done = 0
    slots: list[str | None] = [None] * total
    registered_worktrees = deps.git_svc.list_worktrees(deps.repo_root)

    async def _teardown_one(branch: str, idx: int) -> None:
        nonlocal done
        try:
            if not deps.git_svc.is_ancestor(branch, deps.repo_root):
                return
            worktree_path_ = worktree_identity(branch, deps.repo_root).path
            if worktree_path_ in registered_worktrees:
                try:
                    await asyncio.to_thread(
                        teardown_worktree, deps.git_svc, deps.repo_root, worktree_path_
                    )
                except Exception as e:
                    deps.status_display.print(
                        "Merge",
                        f"Warning: could not remove worktree for {branch!r}: {e}",
                        "warning",
                    )

            try:
                await asyncio.to_thread(
                    deps.git_svc.delete_branch, branch, deps.repo_root
                )
                slots[idx] = branch
            except GitCommandError as e:
                deps.status_display.print(
                    "Merge",
                    f"Warning: could not delete branch {branch!r}: {e}",
                    "warning",
                )
        finally:
            done += 1
            if on_progress is not None:
                on_progress(done, total)

    results = await asyncio.gather(
        *[_teardown_one(b, i) for i, b in enumerate(branches)],
        return_exceptions=True,
    )
    for branch, r in zip(branches, results, strict=True):
        if isinstance(r, BaseException):
            deps.status_display.print(
                "Merge",
                f"Warning: teardown of {branch!r} failed: {r}",
                "warning",
            )
    return [s for s in slots if s is not None]


async def _close_issues_parallel(
    issues: list[dict],
    github_svc: GithubService,
    on_progress: Callable[[int, int], None] | None = None,
    on_error: Callable[[int, BaseException], None] | None = None,
) -> None:
    n = len(issues)
    done = 0

    async def _close_one(issue: dict) -> None:
        nonlocal done
        await asyncio.to_thread(github_svc.close_issue, issue["number"])
        done += 1
        if on_progress is not None:
            on_progress(done, n)

    results = await asyncio.gather(
        *[_close_one(i) for i in issues], return_exceptions=True
    )
    for issue, r in zip(issues, results, strict=True):
        if isinstance(r, BaseException):
            if on_error is not None:
                on_error(issue["number"], r)


def _ensure_conflict_branches_are_merged(
    issues: list[dict], path: Path, deps: _MergeDeps
) -> None:
    for issue in issues:
        branch = branch_for(issue["number"])
        if deps.git_svc.is_ancestor(branch, path):
            continue
        raise RuntimeError(f"{branch} is not a merged branch")


def _merge_sandbox_branch(issue_number: int) -> str:
    return f"{MERGE_SANDBOX_PREFIX}-issue-{issue_number}"


async def merge_phase(completed: list[dict], deps: _MergeDeps) -> MergeResult:
    async with status_row(
        deps.status_display,
        "Merge",
        kind="phase",
        must_close=True,
        initial_phase="Merging",
    ) as row:
        await _wait_for_clean_working_tree(deps, "Merge")
        completed_total = len(completed)

        clean_issues: list[dict] = []
        conflict_issues: list[dict] = []
        for issue in completed:
            if deps.git_svc.try_merge(deps.repo_root, branch_for(issue["number"])):
                clean_issues.append(issue)
            else:
                conflict_issues.append(issue)

        progress = MergeProgressReporter(
            status_display=deps.status_display,
            completed_total=completed_total,
            merge_done=len(clean_issues),
        )
        progress.render()

        def _on_close_error(issue_number: int, exc: BaseException) -> None:
            deps.status_display.print(
                "Merge",
                f"Warning: could not close issue #{issue_number}: {exc}",
                "warning",
            )

        def _close_merge_row(summary: str) -> None:
            row.close("finished")
            deps.status_display.print("Merge", summary, "success")

        async def _close_issues(issues: list[dict]) -> None:
            batch_start = progress.close_done

            def _on_progress(done: int, total: int) -> None:
                progress.update_close_done(batch_start + done)

            await _close_issues_parallel(
                issues, deps.github_svc, _on_progress, _on_close_error
            )

        async def _delete_branches(branches: list[str]) -> list[str]:
            batch_start = progress.remove_done or 0

            def _on_teardown_progress(done: int, total: int) -> None:
                progress.update_remove_done(batch_start + done)

            deleted = await _delete_merged_branches(
                branches, deps, _on_teardown_progress
            )
            progress.update_remove_done(None)
            return deleted

        if clean_issues:
            await _close_issues(clean_issues)
            deps.github_svc.close_completed_parent_issues()

        clean_deleted = await _delete_branches(
            [branch_for(i["number"]) for i in clean_issues]
        )

        if not conflict_issues:
            _close_merge_row(build_merge_close_message(clean_deleted))
        else:
            verdict = await deps.preflight_cache.get_safe_sha(deps)
            if isinstance(verdict, (PreflightHITL, PreflightAFK)):
                deps.status_display.print(
                    "Merge",
                    "Merge-time preflight failed; skipping conflict branch merge. "
                    "Conflict issues remain open for recovery in the next iteration.",
                )
                _close_merge_row(build_merge_close_message(clean_deleted))
                if deps.cfg.auto_push and clean_issues:
                    await deps.git_svc.push(
                        deps.repo_root,
                        resolver=lambda: deps.preflight_cache.pull_with_resolution(
                            deps
                        ),
                    )
                return MergeResult(
                    clean=clean_issues,
                    conflicts=conflict_issues,
                    pending_conflicts=conflict_issues,
                    preflight_blocker=verdict,
                )
            conflict_deleted: list[str] = []
            completed_conflicts: list[dict] = []
            pending_conflicts: list[dict] = []
            for idx, active_issue in enumerate(conflict_issues):
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
                        deps.git_svc.start_merge(
                            sandbox_path, branch_for(active_issue["number"])
                        )
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
                        _ensure_conflict_branches_are_merged(
                            [active_issue], sandbox_path, deps
                        )
                        deps.git_svc.fast_forward_branch(
                            deps.repo_root, target_branch, sandbox_identity.branch
                        )
                        _ensure_conflict_branches_are_merged(
                            [active_issue], deps.repo_root, deps
                        )
                        RoleSession(sandbox_path, AgentRole.MERGER).discard()
                        progress.update_merge_done(progress.merge_done + 1)
                except (
                    AgentTimeoutError,
                    UsageLimitError,
                    TransientAgentError,
                    HardAgentError,
                ):
                    raise
                except Exception as exc:
                    deps.status_display.print(
                        "Merge",
                        f"Conflict branch {branch_for(active_issue['number'])} failed and remains pending: {exc}",
                        "warning",
                    )
                    pending_conflicts = conflict_issues[idx:]
                    break
                conflict_deleted.extend(
                    await _delete_branches([branch_for(active_issue["number"])])
                )
                await _close_issues([active_issue])
                completed_conflicts.append(active_issue)
            if completed_conflicts:
                deps.github_svc.close_completed_parent_issues()
            _close_merge_row(
                build_merge_close_message(
                    clean_deleted + conflict_deleted,
                    completed_conflicts=completed_conflicts,
                    pending_conflicts=pending_conflicts,
                )
            )

        if (
            deps.cfg.auto_push
            and (clean_issues or conflict_issues)
            and not (conflict_issues and pending_conflicts)
        ):
            await deps.git_svc.push(
                deps.repo_root,
                resolver=lambda: deps.preflight_cache.pull_with_resolution(deps),
            )
        return MergeResult(
            clean=clean_issues,
            conflicts=conflict_issues,
            completed_conflicts=completed_conflicts if conflict_issues else [],
            pending_conflicts=pending_conflicts if conflict_issues else [],
        )
