import platform

import pytest
from click.testing import CliRunner

from pycastle.commands.host_check_run import HostCheckRunFailed, HostCheckRunPassed
from pycastle.config import Config


def test_check_keeps_adr_0036_terminal_ordering_contract(tmp_path, monkeypatch):
    import pycastle.commands.check as check_mod
    from pycastle.main import main as cli

    async def fake_run_host_check_command(**kwargs):
        status_display = kwargs["status_display"]
        status_display.register("Host Check", "phase")
        status_display.print("Host Check", "tests")
        status_display.remove("Host Check")
        return HostCheckRunPassed(checked_sha="checked-sha")

    monkeypatch.setattr(check_mod, "run_host_check_loop", fake_run_host_check_command)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["check"])

    assert result.exit_code == 0, result.output
    assert result.output.index("[Host Check] finished") < result.output.index(
        "Host checks passed on "
    )


def test_check_prints_passed_summary_from_host_check_command(
    tmp_path, monkeypatch, capsys
):
    import pycastle.commands.check as check_mod

    async def fake_run_host_check_command(**kwargs):
        return HostCheckRunPassed(checked_sha="checked-sha")

    monkeypatch.setattr(check_mod, "run_host_check_loop", fake_run_host_check_command)
    monkeypatch.chdir(tmp_path)

    check_mod.main(cfg=Config())

    assert capsys.readouterr().out == (
        "Host checks passed on "
        f"{platform.system()} ({platform.platform()}) at checked-sha.\n"
    )


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

    monkeypatch.setattr(check_mod, "run_host_check_loop", fake_run_host_check_run)
    monkeypatch.chdir(tmp_path)

    check_mod.main(cfg=Config())

    assert capsys.readouterr().out == "Host checks filed or updated issues: #41, #42\n"


def test_check_reports_the_checked_sha_from_run_module(tmp_path, monkeypatch, capsys):
    import pycastle.commands.check as check_mod

    async def fake_run_host_check_run(**kwargs):
        return HostCheckRunPassed(checked_sha="checked-sha")

    monkeypatch.setattr(check_mod, "run_host_check_loop", fake_run_host_check_run)
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

    monkeypatch.setattr(check_mod, "run_host_check_loop", fake_run_host_check_run)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(
        RuntimeError, match="Working tree must be clean before running host checks."
    ):
        check_mod.main(cfg=Config())

    assert capsys.readouterr().out == ""
