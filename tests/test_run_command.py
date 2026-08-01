from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from pycastle.config import Config

# ── Shared helpers ─────────────────────────────────────────────────────────────


def _make_docker_svc():
    svc = MagicMock()
    svc.build_image.return_value = None
    return svc


@contextlib.contextmanager
def _run_patches(cfg, fake_docker_svc):
    async def _fake_runtime(*args, **kwargs):
        pass

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.init.refresh"),
        patch("pycastle.commands.build.DockerService", return_value=fake_docker_svc),
        patch("pycastle.main.agent_runtime.run", _fake_runtime),
    ):
        yield


def _setup_project(tmp_path, monkeypatch):
    """Create a minimal pycastle project in tmp_path and return PYCASTLE_HOME."""
    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "pycastle_home"
    monkeypatch.setenv("PYCASTLE_HOME", str(home))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)
    return home


# ── Behavior 1: run slot artifacts created ─────────────────────────────────────


def test_run_cmd_creates_run_slot_artifacts_in_pycastle_home(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    home = _setup_project(tmp_path, monkeypatch)
    cfg = Config(docker_image_name="img")
    fake_svc = _make_docker_svc()

    with _run_patches(cfg, fake_svc):
        result = CliRunner().invoke(cli, ["run", "--no-improve"])

    assert result.exit_code == 0, result.output
    assert (home / ".run.lock").exists(), "global run lock file must exist"
    assert (home / ".runs").is_dir(), "run markers dir must exist"


# ── Behavior 2: second invocation exits 1 with project name ───────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl locking is Unix-only")
def test_run_cmd_second_invocation_exits_1_with_project_name(tmp_path, monkeypatch):
    import fcntl
    import re

    from pycastle.main import main as cli

    home = _setup_project(tmp_path, monkeypatch)
    home.mkdir(parents=True)
    runs_dir = home / ".runs"
    runs_dir.mkdir(parents=True)
    sanitized = re.sub(r"[^a-z0-9]+", "-", tmp_path.name.lower()).strip("-")
    marker = runs_dir / f"{sanitized}.lock"
    marker.touch()

    holder = open(marker, "r+b")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

    cfg = Config(docker_image_name="img")
    fake_svc = _make_docker_svc()

    try:
        with _run_patches(cfg, fake_svc):
            result = CliRunner().invoke(cli, ["run", "--no-improve"])
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    assert result.exit_code == 1
    assert sanitized in result.output


# ── Behavior 3: waiting for another project prints what it's waiting for ───────


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl locking is Unix-only")
def test_run_cmd_reports_waiting_for_other_project_then_proceeds(tmp_path, monkeypatch):
    import fcntl

    from pycastle.main import main as cli

    home = _setup_project(tmp_path, monkeypatch)
    home.mkdir(parents=True)
    runs_dir = home / ".runs"
    runs_dir.mkdir(parents=True)

    other_marker = runs_dir / "other-project.lock"
    other_marker.touch()

    holder = open(other_marker, "r+b")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

    cfg = Config(docker_image_name="img")
    fake_svc = _make_docker_svc()

    result_holder: list = []
    completed = threading.Event()

    def _run_in_thread():
        with _run_patches(cfg, fake_svc):
            r = CliRunner().invoke(cli, ["run", "--no-improve"])
        result_holder.append(r)
        completed.set()

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()

    # Should not complete while other project lock is held.
    assert not completed.wait(0.3), "run should wait while another project is locked"

    # Release other project's lock — run should proceed.
    fcntl.flock(holder, fcntl.LOCK_UN)
    holder.close()
    assert completed.wait(10), (
        "run should complete after other project lock is released"
    )
    t.join(timeout=10)

    result = result_holder[0]
    assert result.exit_code == 0, result.output
    assert "other-project" in result.output


# ── Behavior 4: timeout after six hours exits 1 ───────────────────────────────


def test_run_cmd_timeout_exits_1_with_message(tmp_path, monkeypatch):
    from pycastle.errors import RunSlotTimeoutError
    from pycastle.main import main as cli

    _setup_project(tmp_path, monkeypatch)

    class _TimeoutSlot:
        def __enter__(self):
            raise RunSlotTimeoutError("Timed out waiting for global run lock")

        def __exit__(self, *a):
            pass

    with patch("pycastle.run_lock.run_slot", lambda *a, **kw: _TimeoutSlot()):
        result = CliRunner().invoke(cli, ["run", "--no-improve"])

    assert result.exit_code == 1
    output = result.output.lower()
    assert "timed out" in output or "timeout" in output


# ── Behavior 5: no pycastle/ dir exits 1 before creating lock artifacts ───────


def test_run_cmd_exits_1_before_lock_when_pycastle_dir_absent(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    home = tmp_path / "pycastle_home"
    monkeypatch.setenv("PYCASTLE_HOME", str(home))

    result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert "pycastle init" in result.output
    assert not (home / ".run.lock").exists()
    assert not (home / ".runs").exists()


# ── Behavior 6: refresh called before config load ─────────────────────────────


def test_run_cmd_calls_refresh_before_loading_config(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    _setup_project(tmp_path, monkeypatch)
    cfg = Config(docker_image_name="img")
    fake_svc = _make_docker_svc()
    call_order: list[str] = []

    def _fake_refresh():
        call_order.append("refresh")

    async def _fake_runtime(*args, **kwargs):
        call_order.append("runtime")

    def _fake_load_config():
        call_order.append("load_config")
        return cfg

    with (
        patch("pycastle.main.load_config", _fake_load_config),
        patch("pycastle.commands.init.refresh", _fake_refresh),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.main.agent_runtime.run", _fake_runtime),
    ):
        result = CliRunner().invoke(cli, ["run", "--no-improve"])

    assert result.exit_code == 0, result.output
    assert call_order.index("refresh") < call_order.index("load_config")
    assert call_order.index("load_config") < call_order.index("runtime")


# ── Behavior 7: refresh failure stops run before runtime ──────────────────────


def test_run_cmd_refresh_failure_stops_run_before_runtime(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    _setup_project(tmp_path, monkeypatch)
    cfg = Config(docker_image_name="img")
    fake_svc = _make_docker_svc()
    runtime_called: list[bool] = []

    def _failing_refresh():
        sys.exit(1)

    async def _fake_runtime(*args, **kwargs):
        runtime_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.init.refresh", _failing_refresh),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.main.agent_runtime.run", _fake_runtime),
    ):
        result = CliRunner().invoke(cli, ["run", "--no-improve"])

    assert result.exit_code != 0
    assert not runtime_called


# ── Behavior 8: log sweep after run using post-refresh config ─────────────────


def test_run_cmd_sweeps_old_logs_after_run(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    home = tmp_path / "pycastle_home"
    monkeypatch.setenv("PYCASTLE_HOME", str(home))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    logs_dir = tmp_path / "pycastle" / "logs"
    logs_dir.mkdir(parents=True)
    old_log = logs_dir / "old.log"
    old_log.write_text("ancient\n")
    old_mtime = time.time() - 31 * 24 * 3600
    os.utime(old_log, (old_mtime, old_mtime))
    recent_log = logs_dir / "recent.log"
    recent_log.write_text("fresh\n")
    recent_mtime = time.time()
    os.utime(recent_log, (recent_mtime, recent_mtime))

    cfg = Config(docker_image_name="img", logs_dir=Path("pycastle/logs"))
    fake_svc = _make_docker_svc()

    with _run_patches(cfg, fake_svc):
        result = CliRunner().invoke(cli, ["run", "--no-improve"])

    assert result.exit_code == 0, result.output
    assert not old_log.exists()
    assert recent_log.exists()


def test_run_cmd_sweeps_logs_in_effective_dir_after_refresh_updates_logs_dir(
    tmp_path, monkeypatch
):
    from pycastle.config.loader import derive_docker_image_name
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("PYCASTLE_HOME", str(home))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    home.mkdir()
    (home / "config.py").write_text(
        "from pathlib import Path\nlogs_dir = Path('old-logs')\n"
    )
    pycastle_dir = tmp_path / "pycastle"
    pycastle_dir.mkdir()
    (pycastle_dir / "config.py").write_text("docker_image_name = 'img'\n")

    refreshed_logs_dir = tmp_path / "new-logs" / derive_docker_image_name(tmp_path.name)
    refreshed_logs_dir.mkdir(parents=True)
    old_log = refreshed_logs_dir / "old.log"
    old_log.write_text("ancient\n")
    old_mtime = time.time() - 31 * 24 * 3600
    os.utime(old_log, (old_mtime, old_mtime))

    fake_svc = _make_docker_svc()

    def _refresh():
        (home / "config.py").write_text(
            "from pathlib import Path\nlogs_dir = Path('new-logs')\n"
        )

    async def _fake_runtime(*args, **kwargs):
        pass

    with (
        patch("pycastle.commands.init.refresh", _refresh),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.main.agent_runtime.run", _fake_runtime),
    ):
        result = CliRunner().invoke(cli, ["run", "--no-improve"])

    assert result.exit_code == 0, result.output
    assert not old_log.exists()


# ── Behavior 9: --improve and --no-improve mutually exclusive before waiting ───


def test_run_cmd_improve_and_no_improve_rejected_before_waiting(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    _setup_project(tmp_path, monkeypatch)

    result = CliRunner().invoke(cli, ["run", "--improve", "--no-improve"])

    assert result.exit_code == 1
    assert "mutually exclusive" in result.output


# ── Behavior 10: layer summary printed before waiting for slot ─────────────────


def test_run_cmd_prints_layer_summary_in_output(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    _setup_project(tmp_path, monkeypatch)
    cfg = Config(docker_image_name="img")
    fake_svc = _make_docker_svc()

    with _run_patches(cfg, fake_svc):
        result = CliRunner().invoke(cli, ["run", "--no-improve"])

    assert result.exit_code == 0, result.output
    assert "Config:" in result.output



# ── Behavior 12: --ignore-global-lock listed in help ─────────────────────────


def test_run_cmd_ignore_global_lock_listed_in_help():
    from pycastle.main import main as cli

    result = CliRunner().invoke(cli, ["run", "--help"])

    assert result.exit_code == 0
    assert "--ignore-global-lock" in result.output


# ── Behavior 13: --ignore-global-lock starts immediately while another project runs ──


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl locking is Unix-only")
def test_run_cmd_ignore_global_lock_bypasses_wait_for_other_project(
    tmp_path, monkeypatch
):
    import fcntl

    from pycastle.main import main as cli

    home = _setup_project(tmp_path, monkeypatch)
    home.mkdir(parents=True)
    runs_dir = home / ".runs"
    runs_dir.mkdir(parents=True)

    other_marker = runs_dir / "other-project.lock"
    other_marker.touch()

    holder = open(other_marker, "r+b")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

    cfg = Config(docker_image_name="img")
    fake_svc = _make_docker_svc()

    try:
        with _run_patches(cfg, fake_svc):
            result = CliRunner().invoke(
                cli, ["run", "--no-improve", "--ignore-global-lock"]
            )
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    assert result.exit_code == 0, result.output


# ── Behavior 14: --ignore-global-lock still aborts if own project is running ──


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl locking is Unix-only")
def test_run_cmd_ignore_global_lock_aborts_if_own_project_running(
    tmp_path, monkeypatch
):
    import fcntl
    import re

    from pycastle.main import main as cli

    home = _setup_project(tmp_path, monkeypatch)
    home.mkdir(parents=True)
    runs_dir = home / ".runs"
    runs_dir.mkdir(parents=True)
    sanitized = re.sub(r"[^a-z0-9]+", "-", tmp_path.name.lower()).strip("-")
    marker = runs_dir / f"{sanitized}.lock"
    marker.touch()

    holder = open(marker, "r+b")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

    cfg = Config(docker_image_name="img")
    fake_svc = _make_docker_svc()

    try:
        with _run_patches(cfg, fake_svc):
            result = CliRunner().invoke(
                cli, ["run", "--no-improve", "--ignore-global-lock"]
            )
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    assert result.exit_code == 1
    assert sanitized in result.output


# ── Behavior 15: --ignore-global-lock composes with improve flags ─────────────


def test_run_cmd_ignore_global_lock_composes_with_no_improve(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    _setup_project(tmp_path, monkeypatch)
    cfg = Config(docker_image_name="img")
    fake_svc = _make_docker_svc()

    with _run_patches(cfg, fake_svc):
        result = CliRunner().invoke(
            cli, ["run", "--ignore-global-lock", "--no-improve"]
        )

    assert result.exit_code == 0, result.output
