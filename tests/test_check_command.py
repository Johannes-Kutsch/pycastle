import platform
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")


def test_check_refreshes_branch_runs_host_checks_and_cleans_up(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", remote], check=True, capture_output=True)

    seed = tmp_path / "seed"
    _init_repo(seed)
    (seed / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.1.0'\n"
    )
    (seed / "checked.txt").write_text("stale\n")
    _git(seed, "add", "pyproject.toml", "checked.txt")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")

    local = tmp_path / "local"
    _git(tmp_path, "clone", "-b", "main", str(remote), str(local))
    _git(local, "config", "user.name", "Test User")
    _git(local, "config", "user.email", "test@example.com")

    updater = tmp_path / "updater"
    _git(tmp_path, "clone", "-b", "main", str(remote), str(updater))
    _git(updater, "config", "user.name", "Test User")
    _git(updater, "config", "user.email", "test@example.com")
    (updater / "checked.txt").write_text("fresh\n")
    _git(updater, "commit", "-am", "refresh checked file")
    _git(updater, "push", "origin", "main")
    refreshed_sha = _git(updater, "rev-parse", "HEAD")

    pycastle_dir = local / "pycastle"
    pycastle_dir.mkdir()
    script = (
        "from pathlib import Path; "
        "text = Path('checked.txt').read_text(); "
        "assert text == 'fresh\\n', text"
    )
    if platform.system() == "Windows":
        command = subprocess.list2cmdline([sys.executable, "-c", script])
    else:
        command = shlex.join([sys.executable, "-c", script])
    (pycastle_dir / "config.py").write_text(
        f'host_checks = (("freshness", {command!r}),)\n'
    )

    monkeypatch.chdir(local)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))

    result = CliRunner().invoke(cli, ["check"])

    assert result.exit_code == 0, result.output
    assert refreshed_sha in result.output
    assert platform.system() in result.output
    assert _git(local, "rev-parse", "HEAD") == refreshed_sha
    assert not (local / "pycastle" / ".worktrees").exists()


def test_check_files_one_host_check_issue_per_failed_command_and_reports_numbers(
    tmp_path, monkeypatch, capsys
):
    import pycastle.commands.check as check_mod
    from pycastle.agents.output_protocol import AgentRole, IssueOutput
    from pycastle.config import Config
    from pycastle.iteration._deps import FakeAgentRunner

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "abc123def456"

    failures = {
        "lint": check_mod.HostCheckFailedError(
            name="lint", command="python -c lint", output="lint broke"
        ),
        "tests": check_mod.HostCheckFailedError(
            name="tests", command="python -c tests", output="tests broke"
        ),
    }

    def fake_run_host_check(name: str, command: str, cwd: Path) -> None:
        exc = failures.get(name)
        if exc is not None:
            raise exc

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    fake_runner = FakeAgentRunner(
        [
            IssueOutput(number=41, labels=["bug", "ready-for-human"]),
            IssueOutput(number=42, labels=["bug", "ready-for-human"]),
        ]
    )
    github_svc = MagicMock()

    monkeypatch.setattr(check_mod, "_run_host_check", fake_run_host_check)
    monkeypatch.setattr(
        check_mod, "transient_worktree", lambda *a, **kw: _TransientWorktree()
    )

    cfg = Config(
        host_checks=(
            ("lint", "python -c lint"),
            ("tests", "python -c tests"),
            ("format", "python -c format"),
        )
    )

    check_mod.main(
        cfg=cfg,
        git_service=git_svc,
        github_service=github_svc,
        agent_runner=fake_runner,
    )

    out = capsys.readouterr().out
    assert "41" in out
    assert "42" in out
    assert len(fake_runner.calls) == 2
    assert all(call.role == AgentRole.PREFLIGHT_ISSUE for call in fake_runner.calls)
    assert [call.scope_args["CHECK_NAME"] for call in fake_runner.calls] == [
        "lint",
        "tests",
    ]
    assert all(
        call.scope_args["CHECKED_SHA"] == "abc123def456" for call in fake_runner.calls
    )
    assert all(
        call.scope_args["HOST_OS"] == platform.system() for call in fake_runner.calls
    )
    assert all(
        call.scope_args["HOST_PLATFORM"] == platform.platform()
        for call in fake_runner.calls
    )


def test_check_passes_raw_failed_command_output_to_host_check_issue_agent(
    tmp_path, monkeypatch
):
    import pycastle.commands.check as check_mod
    from pycastle.agents.output_protocol import IssueOutput
    from pycastle.config import Config
    from pycastle.iteration._deps import FakeAgentRunner
    from pycastle.prompts.pipeline import PromptTemplate

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "abc123def456"

    def fake_run_host_check(name: str, command: str, cwd: Path) -> None:
        raise check_mod.HostCheckFailedError(
            name=name,
            command=command,
            output="traceback line 1\ntraceback line 2",
        )

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    fake_runner = FakeAgentRunner(
        [IssueOutput(number=41, labels=["bug", "ready-for-human"])]
    )
    github_svc = MagicMock()

    monkeypatch.setattr(check_mod, "_run_host_check", fake_run_host_check)
    monkeypatch.setattr(
        check_mod, "transient_worktree", lambda *a, **kw: _TransientWorktree()
    )

    check_mod.main(
        cfg=Config(host_checks=(("lint", "python -c lint"),)),
        git_service=git_svc,
        github_service=github_svc,
        agent_runner=fake_runner,
    )

    call = fake_runner.calls[0]
    assert call.template == PromptTemplate.HOST_CHECK_ISSUE
    assert call.scope_args["OUTPUT"] == "traceback line 1\ntraceback line 2"


def test_check_rejects_afk_host_check_issue_without_slice_mode_label(
    tmp_path, monkeypatch
):
    import pycastle.commands.check as check_mod
    from pycastle.agents.output_protocol import IssueOutput
    from pycastle.config import Config
    from pycastle.iteration._deps import FakeAgentRunner

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "abc123def456"

    def fake_run_host_check(name: str, command: str, cwd: Path) -> None:
        raise check_mod.HostCheckFailedError(
            name=name,
            command=command,
            output="traceback line 1\ntraceback line 2",
        )

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    fake_runner = FakeAgentRunner(
        [IssueOutput(number=41, labels=["bug", "ready-for-agent"])]
    )
    github_svc = MagicMock()

    monkeypatch.setattr(check_mod, "_run_host_check", fake_run_host_check)
    monkeypatch.setattr(
        check_mod, "transient_worktree", lambda *a, **kw: _TransientWorktree()
    )

    with pytest.raises(RuntimeError, match="Host-Check Reporter"):
        check_mod.main(
            cfg=Config(host_checks=(("lint", "python -c lint"),)),
            git_service=git_svc,
            github_service=github_svc,
            agent_runner=fake_runner,
        )


def test_check_rejects_afk_host_check_issue_with_short_body(tmp_path, monkeypatch):
    import pycastle.commands.check as check_mod
    from pycastle.agents.output_protocol import IssueOutput
    from pycastle.config import Config
    from pycastle.iteration._deps import FakeAgentRunner

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "abc123def456"

    def fake_run_host_check(name: str, command: str, cwd: Path) -> None:
        raise check_mod.HostCheckFailedError(
            name=name, command=command, output="command output"
        )

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    fake_runner = FakeAgentRunner(
        [IssueOutput(number=41, labels=["bug", "behavior-slice", "ready-for-agent"])]
    )
    github_svc = MagicMock()
    github_svc.get_issue.return_value = {"body": "short"}

    monkeypatch.setattr(check_mod, "_run_host_check", fake_run_host_check)
    monkeypatch.setattr(
        check_mod, "transient_worktree", lambda *a, **kw: _TransientWorktree()
    )

    with pytest.raises(RuntimeError, match="body is"):
        check_mod.main(
            cfg=Config(host_checks=(("lint", "python -c lint"),)),
            git_service=git_svc,
            github_service=github_svc,
            agent_runner=fake_runner,
        )


def test_check_prints_passed_and_files_no_issues_when_all_host_checks_succeed(
    tmp_path, monkeypatch, capsys
):
    import pycastle.commands.check as check_mod
    from pycastle.config import Config

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "abc123def456"

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    monkeypatch.setattr(check_mod, "_run_host_check", lambda *a, **kw: None)
    monkeypatch.setattr(
        check_mod, "transient_worktree", lambda *a, **kw: _TransientWorktree()
    )

    check_mod.main(
        cfg=Config(
            host_checks=(("lint", "python -c lint"), ("tests", "python -c tests"))
        ),
        git_service=git_svc,
    )

    out = capsys.readouterr().out
    assert "abc123def456" in out
    assert "passed" in out
    assert "filed" not in out


def test_check_keeps_passing_host_checks_report_only_after_issue_filing_exists(
    tmp_path, monkeypatch, capsys
):
    import pycastle.commands.check as check_mod
    from pycastle.config import Config

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "abc123def456"

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    github_svc = MagicMock()

    monkeypatch.setattr(check_mod, "_run_host_check", lambda *a, **kw: None)
    monkeypatch.setattr(
        check_mod, "transient_worktree", lambda *a, **kw: _TransientWorktree()
    )

    check_mod.main(
        cfg=Config(host_checks=(("tests", "python -c tests"),)),
        git_service=git_svc,
        github_service=github_svc,
    )

    assert (
        capsys.readouterr().out == "Host checks passed on "
        f"{platform.system()} ({platform.platform()}) at abc123def456.\n"
    )
    github_svc.close_issue.assert_not_called()
    github_svc.add_label_to_issue.assert_not_called()
    github_svc.remove_label_from_issue.assert_not_called()
    github_svc.post_comment.assert_not_called()
