from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Callable, TypeAlias

from ..infrastructure.worktree import transient_worktree

from ..services import GitService


@dataclass(frozen=True)
class HostCheckFailure:
    name: str
    command: str
    output: str


@dataclass(frozen=True)
class HostCheckRunPassed:
    checked_sha: str


@dataclass(frozen=True)
class HostCheckRunFailed:
    checked_sha: str
    failures: tuple[HostCheckFailure, ...]
    issue_numbers: tuple[int, ...]


HostCheckRunOutcome: TypeAlias = HostCheckRunPassed | HostCheckRunFailed


@dataclass
class _CheckDeps:
    repo_root: Path
    git_svc: GitService


def _run_host_check(name: str, command: str, cwd: Path) -> None:
    result = subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        text=True,
        capture_output=True,
    )
    if result.returncode == 0:
        return
    output = (result.stdout + result.stderr).strip()
    raise RuntimeError(f"Host check {name!r} failed: {command}\n{output}")


def _failure_from_exception(
    name: str, command: str, exc: RuntimeError
) -> HostCheckFailure:
    text = str(exc)
    if "\n" in text:
        _, output = text.split("\n", 1)
    else:
        output = ""
    return HostCheckFailure(name=name, command=command, output=output.strip())


def prepare_host_check_run(
    *, git_svc: GitService, repo_root: Path | None = None
) -> str:
    resolved_repo_root = repo_root or Path(".").resolve()
    git_svc.pull_with_merge_fallback(resolved_repo_root)
    if not git_svc.is_working_tree_clean(resolved_repo_root):
        raise RuntimeError("Working tree must be clean before running host checks.")
    return git_svc.get_head_sha(resolved_repo_root)


async def run_host_check_run(
    *,
    host_checks: tuple[tuple[str, str], ...],
    git_svc: GitService,
    repo_root: Path | None = None,
    on_check_start: Callable[[str], None] | None = None,
    run_host_check: Callable[[str, str, Path], None] | None = None,
    transient_worktree_factory: (
        Callable[..., AbstractAsyncContextManager[Path]] | None
    ) = None,
) -> HostCheckRunOutcome:
    resolved_repo_root = repo_root or Path(".").resolve()
    checked_sha = prepare_host_check_run(git_svc=git_svc, repo_root=resolved_repo_root)
    deps = _CheckDeps(repo_root=resolved_repo_root, git_svc=git_svc)
    execute_host_check = run_host_check or _run_host_check
    create_transient_worktree = transient_worktree_factory or transient_worktree

    async with create_transient_worktree(
        f"host-check-{checked_sha[:7]}", sha=checked_sha, deps=deps
    ) as path:
        failures: list[HostCheckFailure] = []
        for name, command in host_checks:
            if on_check_start is not None:
                on_check_start(name)
            try:
                execute_host_check(name, command, path)
            except RuntimeError as exc:
                failures.append(_failure_from_exception(name, command, exc))
        if failures:
            return HostCheckRunFailed(
                checked_sha=checked_sha,
                failures=tuple(failures),
                issue_numbers=(),
            )
        return HostCheckRunPassed(checked_sha=checked_sha)
