import platform
import shlex
import subprocess
import sys
from pathlib import Path

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
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
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
