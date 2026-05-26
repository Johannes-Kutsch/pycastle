import contextlib
import fcntl
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from pycastle.config import Config


# ── Helpers ──────────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _cron_patches(cfg, fake_docker_svc):
    async def _fake_run(*args, **kwargs):
        pass

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.init.refresh"),
        patch("pycastle.commands.build.DockerService", return_value=fake_docker_svc),
        patch("pycastle.iteration.orchestrator.run", _fake_run),
    ):
        yield


def _make_docker_svc():
    svc = MagicMock()
    svc.build_image.return_value = None
    return svc


# ── Behavior 1: lock file at $PYCASTLE_HOME/.cron.lock ───────────────────────


def test_cron_cmd_creates_lock_file_at_pycastle_home(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    home = tmp_path / "pycastle_home"
    monkeypatch.setenv("PYCASTLE_HOME", str(home))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(docker_image_name="img")
    fake_svc = _make_docker_svc()

    with _cron_patches(cfg, fake_svc):
        result = CliRunner().invoke(cli, ["cron", "--no-improve"])

    assert result.exit_code == 0, result.output
    assert (home / ".cron.lock").exists()


def test_cron_cmd_second_invocation_blocks_until_lock_released(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    home = tmp_path / "pycastle_home"
    home.mkdir(parents=True)
    lock_path = home / ".cron.lock"
    lock_path.touch()
    monkeypatch.setenv("PYCASTLE_HOME", str(home))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    # Acquire the lock from the test so the cron invocation will block.
    holder = open(lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

    cfg = Config(docker_image_name="img")
    fake_svc = _make_docker_svc()

    completed = threading.Event()

    def _run_cron():
        with _cron_patches(cfg, fake_svc):
            CliRunner().invoke(cli, ["cron", "--no-improve"])
        completed.set()

    t = threading.Thread(target=_run_cron, daemon=True)
    t.start()

    # cron must be blocked; it should NOT complete within 200 ms.
    assert not completed.wait(0.2), "cron should be blocked while lock is held"

    # Release the lock — cron must now complete.
    fcntl.flock(holder, fcntl.LOCK_UN)
    holder.close()
    assert completed.wait(10), "cron should complete after lock is released"
    t.join(timeout=10)


# ── Behavior 2: init --refresh then run; init failure stops run ───────────────


def test_cron_cmd_calls_refresh_before_orchestrator(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(docker_image_name="img")
    fake_svc = _make_docker_svc()
    call_order: list[str] = []

    def _fake_refresh():
        call_order.append("refresh")

    async def _fake_run(*args, **kwargs):
        call_order.append("orchestrator")

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.init.refresh", _fake_refresh),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.iteration.orchestrator.run", _fake_run),
    ):
        result = CliRunner().invoke(cli, ["cron", "--no-improve"])

    assert result.exit_code == 0, result.output
    assert call_order == ["refresh", "orchestrator"]


def test_cron_cmd_skips_orchestrator_when_refresh_fails(tmp_path, monkeypatch):
    import sys

    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(docker_image_name="img")
    fake_svc = _make_docker_svc()
    orchestrator_called: list[bool] = []

    def _failing_refresh():
        sys.exit(1)

    async def _fake_run(*args, **kwargs):
        orchestrator_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.init.refresh", _failing_refresh),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.iteration.orchestrator.run", _fake_run),
    ):
        result = CliRunner().invoke(cli, ["cron", "--no-improve"])

    assert result.exit_code != 0
    assert not orchestrator_called


# ── Behavior 3: --no-improve forwards to run ─────────────────────────────────


def test_cron_cmd_no_improve_passes_none_improve_mode_to_orchestrator(
    tmp_path, monkeypatch
):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(docker_image_name="img", improve_mode="until_sleep")
    fake_svc = _make_docker_svc()
    captured: dict = {}

    async def _fake_run(*args, **kwargs):
        captured["improve_mode"] = kwargs.get("improve_mode")

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.init.refresh"),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.iteration.orchestrator.run", _fake_run),
    ):
        result = CliRunner().invoke(cli, ["cron", "--no-improve"])

    assert result.exit_code == 0, result.output
    assert captured["improve_mode"] is None


# ── Behavior 4: default improve behavior from config ─────────────────────────


def test_cron_cmd_without_flags_uses_config_improve_mode(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(docker_image_name="img", improve_mode="until_sleep")
    fake_svc = _make_docker_svc()
    captured: dict = {}

    async def _fake_run(*args, **kwargs):
        captured["improve_mode"] = kwargs.get("improve_mode")

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.init.refresh"),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.iteration.orchestrator.run", _fake_run),
    ):
        result = CliRunner().invoke(cli, ["cron"])

    assert result.exit_code == 0, result.output
    assert captured["improve_mode"] == "until_sleep"


def test_cron_cmd_sweeps_old_logs_after_run(tmp_path, monkeypatch):
    """cron_cmd must perform log maintenance — removing *.log files older than 30 days."""
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "home"))
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

    cfg = Config(docker_image_name="img", logs_dir=Path("pycastle/logs"))
    fake_svc = _make_docker_svc()

    with _cron_patches(cfg, fake_svc):
        result = CliRunner().invoke(cli, ["cron", "--no-improve"])

    assert result.exit_code == 0, result.output
    assert not old_log.exists()
    assert recent_log.exists()


def test_cron_cmd_without_flags_uses_none_when_config_improve_mode_not_set(
    tmp_path, monkeypatch
):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(docker_image_name="img")
    fake_svc = _make_docker_svc()
    captured: dict = {}

    async def _fake_run(*args, **kwargs):
        captured["improve_mode"] = kwargs.get("improve_mode")

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.init.refresh"),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.iteration.orchestrator.run", _fake_run),
    ):
        result = CliRunner().invoke(cli, ["cron"])

    assert result.exit_code == 0, result.output
    assert captured["improve_mode"] is None
