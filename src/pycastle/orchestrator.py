import asyncio
import shutil
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent_result import PreflightFailure
from .claude_service import ClaudeService
from .config import load_config
from .container_runner import run_agent as _default_run_agent
from .git_service import GitCommandError, GitService
from .github_service import GithubService
from .iteration import (
    AbortedHITL,
    AbortedUsageLimit,
    Continue,
    Done,
    run_iteration,
)
from .iteration._deps import Deps as IterationDeps


class FileLogger:
    def __init__(self, logs_dir: Path) -> None:
        self._logs_dir = logs_dir

    def log_error(self, issue: dict, error: Exception | PreflightFailure) -> None:
        if isinstance(error, PreflightFailure):
            return
        tb = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        timestamp = datetime.now(timezone.utc).isoformat()
        entry = f"--- {timestamp} ---\n{tb}\n"
        print(entry, file=sys.stderr)
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        with open(self._logs_dir / "errors.log", "a", encoding="utf-8") as f:
            f.write(entry)

    def log_agent_output(self, agent_name: str, output: str) -> None:
        pass


def prune_orphan_worktrees(
    repo_root: Path, git_service: GitService | None = None
) -> None:
    worktrees_dir = repo_root / "pycastle" / ".worktrees"
    if not worktrees_dir.exists():
        return
    svc = git_service or GitService()
    active = {str(p) for p in svc.list_worktrees(repo_root)}
    for child in worktrees_dir.iterdir():
        if str(child.resolve()) not in active and child.is_dir():
            shutil.rmtree(child)


def delete_merged_branches(
    branches: list[str], repo_root: Path, git_service: GitService | None = None
) -> None:
    svc = git_service or GitService()
    for branch in branches:
        if not svc.is_ancestor(branch, repo_root):
            continue
        try:
            svc.delete_branch(branch, repo_root)
            print(f"Deleted merged branch: {branch}")
        except GitCommandError as e:
            print(f"Warning: could not delete branch {branch!r}: {e}", file=sys.stderr)


def _get_repo(repo_root: Path) -> str:
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        capture_output=True,
        cwd=repo_root,
    )
    if result.returncode != 0:
        raise RuntimeError("Could not determine GitHub repo name via gh")
    return result.stdout.decode("utf-8").strip()


def _stage_for_agent(name: str) -> str:
    if name == "Planner":
        return "plan"
    if name.startswith("Implementer"):
        return "implement"
    if name.startswith("Reviewer"):
        return "review"
    if name == "Merger":
        return "merge"
    return ""


async def wait_for_clean_working_tree(repo_root: Path, git_svc: GitService) -> None:
    if git_svc.is_working_tree_clean(repo_root):
        return
    print(
        "Working tree has uncommitted changes. "
        "Please commit or revert all local changes before the merge phase can proceed."
    )
    while not git_svc.is_working_tree_clean(repo_root):
        await asyncio.sleep(10)


async def run(
    env: dict[str, str],
    repo_root: Path,
    *,
    run_agent: Any | None = None,
    claude_service: ClaudeService | None = None,
    git_service: GitService | None = None,
    github_service: GithubService | None = None,
) -> None:
    cfg = load_config(repo_root=repo_root, validate=True, claude_service=claude_service)
    _run_agent = run_agent or _default_run_agent
    prune_orphan_worktrees(repo_root)
    git_svc = git_service or GitService()
    _lazy_github_svc: GithubService | None = None

    def _get_github_svc() -> GithubService:
        nonlocal _lazy_github_svc
        if _lazy_github_svc is None:
            _lazy_github_svc = github_service or GithubService(
                repo=_get_repo(repo_root)
            )
        return _lazy_github_svc

    for iteration in range(1, cfg.max_iterations + 1):
        print(f"\n=== Iteration {iteration}/{cfg.max_iterations} ===\n")

        if not _get_github_svc().has_open_issues_with_label(cfg.issue_label):
            print(f"No issues with label '{cfg.issue_label}' found. Skipping.")
            break

        deps = IterationDeps(
            env=env,
            repo_root=repo_root,
            git_svc=git_svc,
            github_svc=_get_github_svc(),
            run_agent=_run_agent,
            cfg=cfg,
            logger=FileLogger(cfg.logs_dir),
        )
        outcome = await run_iteration(deps)

        match outcome:
            case Done():
                print(f"No issues with label '{cfg.issue_label}' found. Skipping.")
                break
            case AbortedHITL():
                sys.exit(1)
            case AbortedUsageLimit():
                print(
                    "Usage limit reached. Worktrees preserved."
                    " Run 'pycastle run' again to resume.",
                    file=sys.stderr,
                )
                sys.exit(1)
            case Continue():
                pass

    print("\nAll done.")
