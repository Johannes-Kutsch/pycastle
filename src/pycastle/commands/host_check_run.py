from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

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


def prepare_host_check_run(
    *, git_svc: GitService, repo_root: Path | None = None
) -> str:
    resolved_repo_root = repo_root or Path(".").resolve()
    git_svc.pull_with_merge_fallback(resolved_repo_root)
    if not git_svc.is_working_tree_clean(resolved_repo_root):
        raise RuntimeError("Working tree must be clean before running host checks.")
    return git_svc.get_head_sha(resolved_repo_root)
