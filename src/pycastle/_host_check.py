from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeAlias

from .agents.output_protocol import IssueOutput
from .agents.runner import RunRequest


@dataclass(frozen=True)
class HostCheckCommandResult:
    name: str
    command: str
    output: str


@dataclass(frozen=True)
class HostCheckFailure:
    name: str
    command: str
    output: str


@dataclass(frozen=True)
class HostCheckPassedVerdict:
    checked_sha: str


@dataclass(frozen=True)
class HostCheckIssueFiledVerdict:
    checked_sha: str
    failures: tuple[HostCheckFailure, ...]
    issue_numbers: tuple[int, ...]


HostCheckVerdict: TypeAlias = HostCheckPassedVerdict | HostCheckIssueFiledVerdict


class HostCheckFailedError(RuntimeError):
    def __init__(self, *, name: str, command: str, output: str) -> None:
        self.name = name
        self.command = command
        self.output = output
        detail = f"\n{output}" if output else ""
        super().__init__(f"Host check {name!r} failed: {command}{detail}")


class HostCheckCommandExecutor(Protocol):
    def __call__(self, name: str, command: str, cwd: Path) -> None: ...


class HostCheckGitAdapter(Protocol):
    def pull_with_merge_fallback(self, repo_root: Path) -> None: ...
    def is_working_tree_clean(self, repo_root: Path) -> bool: ...
    def get_head_sha(self, repo_root: Path) -> str: ...
    def get_github_remote_repo(self, repo_root: Path) -> tuple[str, str] | None: ...


class HostCheckGithubAdapter(Protocol):
    def get_issue(self, number: int) -> dict: ...


class HostCheckIssueAgentDispatcher(Protocol):
    async def run(self, request: RunRequest) -> IssueOutput: ...


class HostCheckStatusOutput(Protocol):
    def update_phase(self, name: str, phase: str) -> None: ...
    def print(self, caller: str, message: object, style: str | None = None) -> None: ...


class HostCheckWorktreeDeps(Protocol):
    @property
    def repo_root(self) -> Path: ...

    @property
    def git_svc(self) -> HostCheckGitAdapter: ...


class HostCheckWorktreeFactory(Protocol):
    def __call__(
        self,
        name: str,
        *,
        sha: str,
        deps: HostCheckWorktreeDeps,
    ) -> AbstractAsyncContextManager[Path]: ...
