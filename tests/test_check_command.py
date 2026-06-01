import platform
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from pycastle.commands.host_check_run import HostCheckRunFailed, HostCheckRunPassed
from pycastle.config import Config, StageOverride
from tests.support import RecordingStatusDisplay


def test_check_keeps_adr_0036_terminal_ordering_contract(tmp_path, monkeypatch):
    import pycastle.commands.check as check_mod
    from pycastle.main import main as cli

    async def fake_run_host_check_run(**kwargs):
        status_display = kwargs["status_display"]
        status_display.register("Host Check", "phase")
        status_display.print("Host Check", "tests")
        status_display.remove("Host Check")
        return HostCheckRunPassed(checked_sha="checked-sha")

    monkeypatch.setattr(check_mod, "run_host_check_run", fake_run_host_check_run)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["check"])

    assert result.exit_code == 0, result.output
    assert result.output.index("[Host Check] finished") < result.output.index(
        "Host checks passed on "
    )


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
    from pycastle.agents.runner import AgentRunner

    git_svc = MagicMock()
    status_display = RecordingStatusDisplay()
    cfg = Config()
    captured: dict[str, object] = {}

    async def fake_run_host_check_run(**kwargs):
        captured["issue_deps"] = kwargs["issue_deps_factory"]()
        return HostCheckRunPassed(checked_sha="checked-sha")

    monkeypatch.setattr(check_mod, "run_host_check_run", fake_run_host_check_run)
    monkeypatch.setattr(
        check_mod,
        "load_credential_env",
        lambda **kwargs: {"GH_TOKEN": "token"},
    )
    monkeypatch.setattr(check_mod, "_configured_service_registry", lambda cfg, env: {})
    git_svc.get_github_remote_repo.return_value = ("owner", "repo")
    monkeypatch.chdir(tmp_path)

    check_mod.main(
        cfg=cfg,
        git_service=git_svc,
        status_display=status_display,
    )

    issue_deps = captured["issue_deps"]
    assert issue_deps.cfg is cfg
    assert issue_deps.github_svc.repo == "owner/repo"
    assert isinstance(issue_deps.agent_runner, AgentRunner)
    assert issue_deps.status_display is status_display
    assert issue_deps.reporter_override == cfg.preflight_issue_override


def test_check_uses_service_registry_when_resolving_reporter_override(
    tmp_path, monkeypatch
):
    import pycastle.commands.check as check_mod

    git_svc = MagicMock()
    status_display = RecordingStatusDisplay()
    cfg = Config()
    provided_service_registry = MagicMock()
    github_svc = MagicMock()
    agent_runner = MagicMock()
    reporter_override = StageOverride(
        service="codex",
        model="gpt-5.4-mini",
        effort="medium",
    )
    provided_service_registry.resolve.return_value = reporter_override
    captured: dict[str, object] = {}

    async def fake_run_host_check_run(**kwargs):
        captured["issue_deps"] = kwargs["issue_deps_factory"]()
        return HostCheckRunPassed(checked_sha="checked-sha")

    monkeypatch.setattr(check_mod, "run_host_check_run", fake_run_host_check_run)
    monkeypatch.chdir(tmp_path)

    check_mod.main(
        cfg=cfg,
        git_service=git_svc,
        github_service=github_svc,
        agent_runner=agent_runner,
        status_display=status_display,
        service_registry=provided_service_registry,
    )

    assert captured["issue_deps"].github_svc is github_svc
    assert captured["issue_deps"].agent_runner is agent_runner
    assert captured["issue_deps"].reporter_override is reporter_override
    provided_service_registry.resolve.assert_called_once()


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
