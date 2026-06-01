import platform
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from pycastle.commands.host_check_run import HostCheckRunFailed, HostCheckRunPassed
from pycastle.config import Config, StageOverride
from tests.support import RecordingStatusDisplay


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


def test_check_delegates_host_checks_to_host_check_run(tmp_path, monkeypatch, capsys):
    import pycastle.commands.check as check_mod

    git_svc = MagicMock()
    github_svc = MagicMock()
    agent_runner = MagicMock()
    status_display = RecordingStatusDisplay()
    cfg = Config(host_checks=(("tests", "python -c tests"),))
    captured: dict[str, object] = {}

    async def fake_run_host_check_run(**kwargs):
        captured.update(kwargs)
        return HostCheckRunPassed(checked_sha="checked-sha")

    monkeypatch.setattr(check_mod, "run_host_check_run", fake_run_host_check_run)
    monkeypatch.chdir(tmp_path)

    check_mod.main(
        cfg=cfg,
        git_service=git_svc,
        github_service=github_svc,
        agent_runner=agent_runner,
        status_display=status_display,
    )

    assert captured["host_checks"] == cfg.host_checks
    assert captured["git_svc"] is git_svc
    assert captured["repo_root"] == tmp_path.resolve()
    assert captured["status_display"] is status_display
    issue_deps = captured["issue_deps_factory"]()
    assert issue_deps.cfg is cfg
    assert issue_deps.github_svc is github_svc
    assert issue_deps.agent_runner is agent_runner
    assert issue_deps.status_display is status_display
    assert capsys.readouterr().out == (
        "Host checks passed on "
        f"{platform.system()} ({platform.platform()}) at checked-sha.\n"
    )


def test_check_builds_default_host_check_issue_deps_when_run_module_requests_them(
    tmp_path, monkeypatch
):
    import pycastle.commands.check as check_mod

    git_svc = MagicMock()
    status_display = RecordingStatusDisplay()
    cfg = Config()
    reporter_override = StageOverride(
        service="codex",
        model="gpt-5.4-mini",
        effort="medium",
    )
    resolved_runner = MagicMock()
    resolved_service_registry = MagicMock()
    resolved_github_svc = MagicMock()
    captured: dict[str, object] = {}

    async def fake_run_host_check_run(**kwargs):
        captured["issue_deps"] = kwargs["issue_deps_factory"]()
        return HostCheckRunPassed(checked_sha="checked-sha")

    monkeypatch.setattr(check_mod, "run_host_check_run", fake_run_host_check_run)
    monkeypatch.setattr(
        check_mod,
        "_resolve_agent_runner",
        lambda cfg_arg, git_arg: (resolved_runner, resolved_service_registry),
    )
    monkeypatch.setattr(
        check_mod,
        "_resolve_github_service",
        lambda repo_root, cfg_arg, git_arg: resolved_github_svc,
    )
    monkeypatch.setattr(
        check_mod,
        "_resolve_reporter_override",
        lambda cfg_arg, service_registry: reporter_override,
    )
    monkeypatch.chdir(tmp_path)

    check_mod.main(
        cfg=cfg,
        git_service=git_svc,
        status_display=status_display,
    )

    issue_deps = captured["issue_deps"]
    assert issue_deps.cfg is cfg
    assert issue_deps.github_svc is resolved_github_svc
    assert issue_deps.agent_runner is resolved_runner
    assert issue_deps.status_display is status_display
    assert issue_deps.reporter_override is reporter_override


def test_check_uses_service_registry_when_resolving_reporter_override(
    tmp_path, monkeypatch
):
    import pycastle.commands.check as check_mod

    git_svc = MagicMock()
    status_display = RecordingStatusDisplay()
    cfg = Config()
    provided_service_registry = MagicMock()
    resolved_github_svc = MagicMock()
    captured: dict[str, object] = {}

    async def fake_run_host_check_run(**kwargs):
        captured["issue_deps"] = kwargs["issue_deps_factory"]()
        return HostCheckRunPassed(checked_sha="checked-sha")

    monkeypatch.setattr(check_mod, "run_host_check_run", fake_run_host_check_run)
    monkeypatch.setattr(
        check_mod,
        "_resolve_github_service",
        lambda repo_root, cfg_arg, git_arg: resolved_github_svc,
    )

    def fake_resolve_reporter_override(cfg_arg, service_registry):
        captured["service_registry"] = service_registry
        return cfg_arg.preflight_issue_override

    monkeypatch.setattr(
        check_mod,
        "_resolve_reporter_override",
        fake_resolve_reporter_override,
    )
    monkeypatch.chdir(tmp_path)

    check_mod.main(
        cfg=cfg,
        git_service=git_svc,
        agent_runner=MagicMock(),
        status_display=status_display,
        service_registry=provided_service_registry,
    )

    assert captured["service_registry"] is provided_service_registry
    assert captured["issue_deps"].github_svc is resolved_github_svc


def test_check_prints_host_check_issue_summary_after_failed_run(
    tmp_path, monkeypatch, capsys
):
    import pycastle.commands.check as check_mod

    async def fake_run_host_check_run(**kwargs):
        return HostCheckRunFailed(
            checked_sha="checked-sha",
            failures=(),
            issue_numbers=(41, 42),
        )

    monkeypatch.setattr(check_mod, "run_host_check_run", fake_run_host_check_run)
    monkeypatch.chdir(tmp_path)

    check_mod.main(cfg=Config())

    assert capsys.readouterr().out == "Host checks filed or updated issues: #41, #42\n"


def test_check_reports_the_checked_sha_from_run_module(tmp_path, monkeypatch, capsys):
    import pycastle.commands.check as check_mod

    async def fake_run_host_check_run(**kwargs):
        return HostCheckRunPassed(checked_sha="checked-sha")

    monkeypatch.setattr(check_mod, "run_host_check_run", fake_run_host_check_run)
    monkeypatch.chdir(tmp_path)

    check_mod.main(cfg=Config())

    out = capsys.readouterr().out
    assert "checked-sha" in out
    assert "moved-head" not in out


def test_check_propagates_host_check_run_failures_without_extra_summary(
    tmp_path, monkeypatch, capsys
):
    import pycastle.commands.check as check_mod

    async def fake_run_host_check_run(**kwargs):
        raise RuntimeError("Working tree must be clean before running host checks.")

    monkeypatch.setattr(check_mod, "run_host_check_run", fake_run_host_check_run)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(
        RuntimeError, match="Working tree must be clean before running host checks."
    ):
        check_mod.main(cfg=Config())

    assert capsys.readouterr().out == ""
