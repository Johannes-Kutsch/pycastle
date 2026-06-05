import asyncio
from pathlib import Path
from unittest.mock import MagicMock

from pycastle._host_check import (
    HostCheckFailedError,
    HostCheckFailure,
    HostCheckIssueFiledVerdict,
    HostCheckPassedVerdict,
    run_host_check_loop,
)
from tests.support import RecordingStatusDisplay


def test_run_host_check_loop_surfaces_aggregate_phase_row_before_returning_verdict(
    tmp_path: Path,
) -> None:
    events: list[tuple[object, ...]] = []
    git_svc = MagicMock()

    def fake_pull(repo_root: Path) -> None:
        events.append(("pull", repo_root))

    def fake_clean(repo_root: Path) -> bool:
        events.append(("clean", repo_root))
        return True

    git_svc.pull_with_merge_fallback.side_effect = fake_pull
    git_svc.is_working_tree_clean.side_effect = fake_clean
    git_svc.get_head_sha.return_value = "abc123def456"

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            events.append(("worktree-enter",))
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            events.append(("worktree-exit",))
            return None

    def fake_run_host_check(name: str, command: str, cwd: Path) -> None:
        events.append(("host-check", name, command, cwd))
        return None

    display = RecordingStatusDisplay()

    result = asyncio.run(
        run_host_check_loop(
            host_checks=(("tests", "python -c tests"),),
            git_svc=git_svc,
            repo_root=tmp_path,
            status_display=display,
            run_host_check=fake_run_host_check,
            transient_worktree_factory=lambda *a, **kw: _TransientWorktree(),
        )
    )

    assert result == HostCheckPassedVerdict(checked_sha="abc123def456")
    assert display.calls[0] == (
        "register",
        "Host Check",
        "phase",
        "started",
        "Setup",
        None,
    )
    assert events[:3] == [
        ("pull", tmp_path),
        ("clean", tmp_path),
        ("worktree-enter",),
    ]
    assert ("host-check", "tests", "python -c tests", tmp_path) in events
    assert display.calls[-1] == ("remove", "Host Check", "finished", "success")


def test_run_host_check_loop_closes_host_check_row_before_issue_filing_starts(
    tmp_path: Path,
) -> None:
    events: list[tuple[object, ...]] = []
    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "abc123def456"

    class _RecordingDisplay(RecordingStatusDisplay):
        def remove(
            self,
            caller: str,
            shutdown_message: str = "finished",
            shutdown_style: str = "success",
        ) -> None:
            events.append(("remove", caller, shutdown_message, shutdown_style))
            super().remove(caller, shutdown_message, shutdown_style)

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            events.append(("worktree-enter",))
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            events.append(("worktree-exit",))
            return None

    def fake_run_host_check(name: str, command: str, cwd: Path) -> None:
        raise HostCheckFailedError(name=name, command=command, output="tests broke")

    async def fake_file_issue(
        failure: HostCheckFailure, mount_path: Path, checked_sha: str
    ) -> int:
        events.append(("file-issue", failure.name, mount_path, checked_sha))
        return 41

    result = asyncio.run(
        run_host_check_loop(
            host_checks=(("tests", "python -c tests"),),
            git_svc=git_svc,
            repo_root=tmp_path,
            status_display=_RecordingDisplay(),
            run_host_check=fake_run_host_check,
            transient_worktree_factory=lambda *a, **kw: _TransientWorktree(),
            file_issue_for_failure=fake_file_issue,
        )
    )

    assert result == HostCheckIssueFiledVerdict(
        checked_sha="abc123def456",
        failures=(
            HostCheckFailure(
                name="tests",
                command="python -c tests",
                output="tests broke",
            ),
        ),
        issue_numbers=(41,),
    )
    assert events == [
        ("worktree-enter",),
        ("remove", "Host Check", "failed tests", "error"),
        ("file-issue", "tests", tmp_path, "abc123def456"),
        ("worktree-exit",),
    ]


def test_run_host_check_loop_collects_all_failed_commands_before_reporting_issues(
    tmp_path: Path,
) -> None:
    events: list[tuple[object, ...]] = []
    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "abc123def456"

    class _RecordingDisplay(RecordingStatusDisplay):
        def print(self, caller: str, message: object, style: str | None = None) -> None:
            events.append(("print", caller, message, style))
            super().print(caller, message, style)

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    def fake_run_host_check(name: str, command: str, cwd: Path):
        events.append(("host-check", name, command, cwd))
        if name == "tests":
            raise HostCheckFailedError(
                name=name,
                command=command,
                output="tests broke",
            )
        if name == "lint":
            return None
        raise HostCheckFailedError(
            name=name,
            command=command,
            output="typing broke",
        )

    async def fake_file_issue(
        failure: HostCheckFailure, mount_path: Path, checked_sha: str
    ) -> int:
        events.append(("file-issue", failure.name, mount_path, checked_sha))
        return {"tests": 41, "typecheck": 42}[failure.name]

    result = asyncio.run(
        run_host_check_loop(
            host_checks=(
                ("tests", "python -m pytest"),
                ("lint", "ruff check ."),
                ("typecheck", "mypy ."),
            ),
            git_svc=git_svc,
            repo_root=tmp_path,
            status_display=_RecordingDisplay(),
            run_host_check=fake_run_host_check,
            transient_worktree_factory=lambda *a, **kw: _TransientWorktree(),
            file_issue_for_failure=fake_file_issue,
        )
    )

    assert result == HostCheckIssueFiledVerdict(
        checked_sha="abc123def456",
        failures=(
            HostCheckFailure(
                name="tests",
                command="python -m pytest",
                output="tests broke",
            ),
            HostCheckFailure(
                name="typecheck",
                command="mypy .",
                output="typing broke",
            ),
        ),
        issue_numbers=(41, 42),
    )
    assert events == [
        ("host-check", "tests", "python -m pytest", tmp_path),
        ("host-check", "lint", "ruff check .", tmp_path),
        ("host-check", "typecheck", "mypy .", tmp_path),
        ("print", "Host Check", "failed tests", "error"),
        ("print", "Host Check", "failed typecheck", "error"),
        ("file-issue", "tests", tmp_path, "abc123def456"),
        ("file-issue", "typecheck", tmp_path, "abc123def456"),
    ]
