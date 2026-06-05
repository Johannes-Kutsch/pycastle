import ast
import inspect
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

    monkeypatch.setattr(
        check_mod, "run_host_check_command", fake_run_host_check_command
    )
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

    monkeypatch.setattr(
        check_mod, "run_host_check_command", fake_run_host_check_command
    )
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

    monkeypatch.setattr(check_mod, "run_host_check_command", fake_run_host_check_run)
    monkeypatch.chdir(tmp_path)

    check_mod.main(cfg=Config())

    assert capsys.readouterr().out == "Host checks filed or updated issues: #41, #42\n"


def test_check_reports_the_checked_sha_from_run_module(tmp_path, monkeypatch, capsys):
    import pycastle.commands.check as check_mod

    async def fake_run_host_check_run(**kwargs):
        return HostCheckRunPassed(checked_sha="checked-sha")

    monkeypatch.setattr(check_mod, "run_host_check_command", fake_run_host_check_run)
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

    monkeypatch.setattr(check_mod, "run_host_check_command", fake_run_host_check_run)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(
        RuntimeError, match="Working tree must be clean before running host checks."
    ):
        check_mod.main(cfg=Config())

    assert capsys.readouterr().out == ""


def test_check_delegates_host_check_orchestration_to_run_module_adapter(
    tmp_path, monkeypatch
):
    import pycastle.commands.check as check_mod

    cfg = Config()
    github_service = object()
    agent_runner = object()
    service_registry = object()
    recorded_kwargs: dict[str, object] = {}

    async def fake_run_host_check_command(**kwargs):
        recorded_kwargs.update(kwargs)
        return HostCheckRunPassed(checked_sha="checked-sha")

    monkeypatch.setattr(
        check_mod, "run_host_check_command", fake_run_host_check_command
    )
    monkeypatch.chdir(tmp_path)

    check_mod.main(
        cfg=cfg,
        github_service=github_service,
        agent_runner=agent_runner,
        service_registry=service_registry,
    )

    assert recorded_kwargs == {
        "cfg": cfg,
        "git_svc": recorded_kwargs["git_svc"],
        "repo_root": tmp_path.resolve(),
        "github_svc": github_service,
        "agent_runner": agent_runner,
        "status_display": recorded_kwargs["status_display"],
        "service_registry": service_registry,
    }
    module_tree = ast.parse(inspect.getsource(check_mod))
    main_def = next(
        node
        for node in module_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    referenced_names = {
        node.id for node in ast.walk(main_def) if isinstance(node, ast.Name)
    }

    assert "run_host_check_command" in referenced_names
    assert "run_host_check_loop" not in referenced_names
    assert "create_host_check_issue_filer" not in referenced_names
    assert "resolve_host_check_issue_deps" not in referenced_names
    assert "transient_worktree" not in referenced_names
