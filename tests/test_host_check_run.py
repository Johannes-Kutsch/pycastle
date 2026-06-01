import pytest
from unittest.mock import MagicMock
import asyncio
import platform
import shlex
import subprocess
import sys
from pathlib import Path

from pycastle.commands.host_check_run import HostCheckRunPassed, prepare_host_check_run


def test_prepare_host_check_run_refreshes_before_clean_tree_and_fails_early():
    from pycastle.commands import host_check_run as run_mod

    events: list[tuple[str, object]] = []
    git_svc = MagicMock()

    def fake_pull(repo_root):
        events.append(("pull", repo_root))

    def fake_clean(repo_root):
        events.append(("clean", repo_root))
        return False

    git_svc.pull_with_merge_fallback.side_effect = fake_pull
    git_svc.is_working_tree_clean.side_effect = fake_clean

    with pytest.raises(
        RuntimeError, match="Working tree must be clean before running host checks."
    ):
        run_mod.prepare_host_check_run(git_svc=git_svc)

    assert events == [
        ("pull", run_mod.Path(".").resolve()),
        ("clean", run_mod.Path(".").resolve()),
    ]
    git_svc.get_head_sha.assert_not_called()


def test_prepare_host_check_run_returns_head_sha_when_working_tree_is_clean():
    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "abc123"

    result = prepare_host_check_run(git_svc=git_svc)

    assert result == "abc123"


def test_prepare_host_check_run_passes_explicit_repo_root_to_git_service(tmp_path):
    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "def456"

    result = prepare_host_check_run(git_svc=git_svc, repo_root=tmp_path)

    git_svc.pull_with_merge_fallback.assert_called_once_with(tmp_path)
    git_svc.is_working_tree_clean.assert_called_once_with(tmp_path)
    git_svc.get_head_sha.assert_called_once_with(tmp_path)
    assert result == "def456"


def test_run_host_check_run_executes_passing_checks_in_checked_sha_worktree_and_returns_sha(
    tmp_path, monkeypatch, capsys
):
    from pycastle.commands import host_check_run as run_mod

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "checked-sha"

    (tmp_path / "checked.txt").write_text("fresh\n", encoding="utf-8")

    script = (
        "from pathlib import Path; "
        "assert Path('checked.txt').read_text() == 'fresh\\n'; "
        "print('passing stdout'); "
        "import sys; print('passing stderr', file=sys.stderr)"
    )
    if platform.system() == "Windows":
        command = subprocess.list2cmdline([sys.executable, "-c", script])
    else:
        command = shlex.join([sys.executable, "-c", script])

    transient_calls: list[tuple[str, str, Path]] = []
    surfaced: list[str] = []

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            transient_calls.append(("enter", "checked-sha", tmp_path))
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            transient_calls.append(("exit", "checked-sha", tmp_path))
            return None

    def fake_transient_worktree(name: str, *, sha: str | None, deps):
        assert name == "host-check-checked"
        assert sha == "checked-sha"
        assert deps.repo_root == tmp_path
        assert deps.git_svc is git_svc
        return _TransientWorktree()

    monkeypatch.setattr(run_mod, "transient_worktree", fake_transient_worktree)

    result = asyncio.run(
        run_mod.run_host_check_run(
            host_checks=(("freshness", command),),
            git_svc=git_svc,
            repo_root=tmp_path,
            on_check_start=surfaced.append,
        )
    )

    assert result == HostCheckRunPassed(checked_sha="checked-sha")
    assert surfaced == ["freshness"]
    assert transient_calls == [
        ("enter", "checked-sha", tmp_path),
        ("exit", "checked-sha", tmp_path),
    ]
    out = capsys.readouterr()
    assert "passing stdout" not in out.out
    assert "passing stderr" not in out.out


def test_run_host_check_run_collects_structured_failed_checks_without_leaking_command_text(
    tmp_path,
):
    from pycastle.commands import host_check_run as run_mod

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "checked-sha"

    seen_checks: list[tuple[str, str, Path]] = []
    transient_shas: list[str] = []
    multi_line_command = "python -c lint\npython -c more-lint"

    def fake_run_host_check(name: str, command: str, cwd: Path) -> None:
        seen_checks.append((name, command, cwd))
        if name == "format":
            return
        raise RuntimeError(
            f"Host check {name!r} failed: {command}\n{name} stdout\n{name} stderr"
        )

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    def fake_transient_worktree(name: str, *, sha: str | None, deps):
        assert name == "host-check-checked"
        assert deps.repo_root == tmp_path
        transient_shas.append(sha or "")
        return _TransientWorktree()

    result = asyncio.run(
        run_mod.run_host_check_run(
            host_checks=(
                ("lint", multi_line_command),
                ("format", "python -c format"),
                ("tests", "python -c tests"),
            ),
            git_svc=git_svc,
            repo_root=tmp_path,
            run_host_check=fake_run_host_check,
            transient_worktree_factory=fake_transient_worktree,
        )
    )

    assert result == run_mod.HostCheckRunFailed(
        checked_sha="checked-sha",
        failures=(
            run_mod.HostCheckFailure(
                name="lint",
                command=multi_line_command,
                output="lint stdout\nlint stderr",
            ),
            run_mod.HostCheckFailure(
                name="tests",
                command="python -c tests",
                output="tests stdout\ntests stderr",
            ),
        ),
        issue_numbers=(),
    )
    assert seen_checks == [
        ("lint", multi_line_command, tmp_path),
        ("format", "python -c format", tmp_path),
        ("tests", "python -c tests", tmp_path),
    ]
    assert transient_shas == ["checked-sha"]
