from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
import platform
from typing import Awaitable, Callable, Protocol, TypeAlias

from .agents.output_protocol import IssueOutput
from .agents.runner import RunRequest
from .display.status_display import PlainStatusDisplay, StatusDisplay
from .iteration._rows import status_row


@dataclass(frozen=True)
class HostCheckCommandResult:
    name: str
    command: str
    returncode: int
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


@dataclass(frozen=True)
class HostCheckIssuePayload:
    host_os: str
    host_platform: str
    checked_sha: str
    check_name: str
    command: str
    output: str


HostCheckVerdict: TypeAlias = HostCheckPassedVerdict | HostCheckIssueFiledVerdict


class HostCheckFailedError(RuntimeError):
    def __init__(self, *, name: str, command: str, output: str) -> None:
        self.name = name
        self.command = command
        self.output = output
        detail = f"\n{output}" if output else ""
        super().__init__(f"Host check {name!r} failed: {command}{detail}")


class HostCheckCommandExecutor(Protocol):
    def __call__(
        self, name: str, command: str, cwd: Path
    ) -> HostCheckCommandResult | None: ...


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


HostCheckIssueFiler: TypeAlias = Callable[[HostCheckIssuePayload, Path], Awaitable[int]]


def prepare_host_check_loop(
    *, git_svc: HostCheckGitAdapter, repo_root: Path | None = None
) -> str:
    resolved_repo_root = repo_root or Path(".").resolve()
    git_svc.pull_with_merge_fallback(resolved_repo_root)
    if not git_svc.is_working_tree_clean(resolved_repo_root):
        raise RuntimeError("Working tree must be clean before running host checks.")
    return git_svc.get_head_sha(resolved_repo_root)


def _surface_current_host_check(status_display: StatusDisplay, name: str) -> None:
    status_display.update_phase("Host Check", name)
    if isinstance(status_display, PlainStatusDisplay):
        status_display.print("Host Check", name)


def _surface_failed_host_checks(
    status_display: StatusDisplay, failures: list[HostCheckFailure]
) -> None:
    for failure in failures:
        status_display.print("Host Check", f"failed {failure.name}", style="error")


def _failure_from_command_result(
    command_result: HostCheckCommandResult,
) -> HostCheckFailure:
    return HostCheckFailure(
        name=command_result.name,
        command=command_result.command,
        output=command_result.output,
    )


def _failure_from_exception(
    name: str, command: str, exc: RuntimeError
) -> HostCheckFailure:
    if isinstance(exc, HostCheckFailedError):
        return HostCheckFailure(
            name=exc.name,
            command=exc.command,
            output=exc.output,
        )

    text = str(exc)
    prefix = f"Host check {name!r} failed: {command}"
    output = text.removeprefix(prefix).lstrip("\n")
    return HostCheckFailure(name=name, command=command, output=output)


def _is_failed_command_result(
    command_result: HostCheckCommandResult | None,
) -> bool:
    return command_result is not None and command_result.returncode != 0


async def run_host_check_loop(
    *,
    host_checks: tuple[tuple[str, str], ...],
    git_svc: HostCheckGitAdapter,
    repo_root: Path | None = None,
    status_display: StatusDisplay | None = None,
    on_check_start: Callable[[str], None] | None = None,
    on_failures_detected: Callable[[list[HostCheckFailure]], None] | None = None,
    run_host_check: HostCheckCommandExecutor,
    transient_worktree_factory: HostCheckWorktreeFactory,
    file_issue_for_failure: HostCheckIssueFiler | None = None,
) -> HostCheckVerdict:
    resolved_repo_root = repo_root or Path(".").resolve()
    host_os = platform.system()
    host_platform = platform.platform()

    @dataclass
    class _CheckDeps:
        repo_root: Path
        git_svc: HostCheckGitAdapter

    def _run_configured_host_checks(path: Path) -> list[HostCheckFailure]:
        failures: list[HostCheckFailure] = []
        for name, command in host_checks:
            if status_display is not None:
                _surface_current_host_check(status_display, name)
            if on_check_start is not None:
                on_check_start(name)
            try:
                command_result = run_host_check(name, command, path)
            except RuntimeError as exc:
                failures.append(_failure_from_exception(name, command, exc))
                continue
            if _is_failed_command_result(command_result):
                assert command_result is not None
                failures.append(_failure_from_command_result(command_result))
        if failures and status_display is not None:
            _surface_failed_host_checks(status_display, failures)
        return failures

    async def _verdict_for_failures(
        *, checked_sha: str, path: Path, failures: list[HostCheckFailure]
    ) -> HostCheckVerdict:
        if not failures:
            return HostCheckPassedVerdict(checked_sha=checked_sha)
        if on_failures_detected is not None:
            on_failures_detected(failures)
        issue_numbers: tuple[int, ...] = ()
        if file_issue_for_failure is not None:
            issue_numbers = tuple(
                [
                    await file_issue_for_failure(
                        HostCheckIssuePayload(
                            host_os=host_os,
                            host_platform=host_platform,
                            checked_sha=checked_sha,
                            check_name=failure.name,
                            command=failure.command,
                            output=failure.output,
                        ),
                        path,
                    )
                    for failure in failures
                ]
            )
        return HostCheckIssueFiledVerdict(
            checked_sha=checked_sha,
            failures=tuple(failures),
            issue_numbers=issue_numbers,
        )

    if status_display is None:
        checked_sha = prepare_host_check_loop(
            git_svc=git_svc, repo_root=resolved_repo_root
        )
        deps = _CheckDeps(repo_root=resolved_repo_root, git_svc=git_svc)
        async with transient_worktree_factory(
            f"host-check-{checked_sha[:7]}", sha=checked_sha, deps=deps
        ) as path:
            failures = _run_configured_host_checks(path)
            return await _verdict_for_failures(
                checked_sha=checked_sha, path=path, failures=failures
            )

    async with status_row(
        status_display,
        "Host Check",
        kind="phase",
        must_close=True,
    ) as row:
        checked_sha = prepare_host_check_loop(
            git_svc=git_svc, repo_root=resolved_repo_root
        )
        deps = _CheckDeps(repo_root=resolved_repo_root, git_svc=git_svc)
        async with transient_worktree_factory(
            f"host-check-{checked_sha[:7]}", sha=checked_sha, deps=deps
        ) as path:
            failures = _run_configured_host_checks(path)
            if not failures:
                row.close("finished")
            else:
                row.close(f"failed {failures[0].name}", shutdown_style="error")
            return await _verdict_for_failures(
                checked_sha=checked_sha, path=path, failures=failures
            )
