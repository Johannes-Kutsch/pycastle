import os
import stat
import subprocess
from pathlib import Path

import pytest

DEFAULTS_DIR = Path(__file__).parent.parent / "src" / "pycastle" / "defaults"


@pytest.fixture()
def fake_crontab(tmp_path):
    """Fake crontab shim backed by a tempfile; returns (bin_dir, data_file)."""
    data_file = tmp_path / "crontab_data.txt"
    data_file.write_text("")

    bin_dir = tmp_path / "fake_bin"
    bin_dir.mkdir()

    shim = bin_dir / "crontab"
    shim.write_text(
        f"#!/usr/bin/env bash\n"
        f'DATA="{data_file}"\n'
        f'case "${{1:-}}" in\n'
        f"  -l)\n"
        f'    if [ -s "$DATA" ]; then cat "$DATA"; else echo "no crontab for $(id -un)" >&2; exit 1; fi\n'
        f"    ;;\n"
        f'  -r) : > "$DATA" ;;\n'
        f'  -)  cat > "$DATA" ;;\n'
        f'  *)  echo "fake crontab: unknown arg: $*" >&2; exit 1 ;;\n'
        f"esac\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    return bin_dir, data_file


@pytest.fixture()
def cron_env(tmp_path, fake_crontab):
    """Fake consuming project with cron scripts and fake crontab shim on PATH."""
    bin_dir, data_file = fake_crontab

    pycastle_dir = tmp_path / "pycastle"
    pycastle_dir.mkdir()

    for script in ["cron-install.sh", "cron-uninstall.sh"]:
        dst = pycastle_dir / script
        dst.write_bytes((DEFAULTS_DIR / script).read_bytes())
        dst.chmod(dst.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    return {
        "project_dir": tmp_path,
        "install_sh": pycastle_dir / "cron-install.sh",
        "uninstall_sh": pycastle_dir / "cron-uninstall.sh",
        "env": env,
        "data_file": data_file,
    }


_BASH = subprocess.run(["which", "bash"], capture_output=True, text=True).stdout.strip()


def _run(script: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_BASH, str(script)],
        env=env,
        capture_output=True,
        text=True,
    )


# ── Install ───────────────────────────────────────────────────────────────────


def test_install_creates_entry_with_correct_marker(cron_env):
    result = _run(cron_env["install_sh"], cron_env["env"])

    assert result.returncode == 0, result.stderr
    content = cron_env["data_file"].read_text()
    assert f"# pycastle:{cron_env['project_dir']}" in content
    assert "0 1 * * *" in content


def test_install_is_idempotent(cron_env):
    _run(cron_env["install_sh"], cron_env["env"])
    _run(cron_env["install_sh"], cron_env["env"])

    content = cron_env["data_file"].read_text()
    marker = f"# pycastle:{cron_env['project_dir']}"
    assert content.count(marker) == 1


def test_install_preserves_other_cron_lines(cron_env):
    other = "0 2 * * * /other/job.sh # unrelated"
    cron_env["data_file"].write_text(other + "\n")

    _run(cron_env["install_sh"], cron_env["env"])

    content = cron_env["data_file"].read_text()
    assert other in content
    assert f"# pycastle:{cron_env['project_dir']}" in content


# ── Uninstall ─────────────────────────────────────────────────────────────────


def test_uninstall_removes_matching_line(cron_env):
    _run(cron_env["install_sh"], cron_env["env"])
    assert f"# pycastle:{cron_env['project_dir']}" in cron_env["data_file"].read_text()

    result = _run(cron_env["uninstall_sh"], cron_env["env"])

    assert result.returncode == 0, result.stderr
    assert (
        f"# pycastle:{cron_env['project_dir']}" not in cron_env["data_file"].read_text()
    )


def test_uninstall_is_noop_when_no_matching_line(cron_env):
    result = _run(cron_env["uninstall_sh"], cron_env["env"])

    assert result.returncode == 0, result.stderr


def test_uninstall_leaves_other_lines_intact(cron_env):
    other = "0 2 * * * /other/repo/pycastle/cron.sh # pycastle:/other/repo"
    cron_env["data_file"].write_text(other + "\n")

    _run(cron_env["install_sh"], cron_env["env"])
    _run(cron_env["uninstall_sh"], cron_env["env"])

    remaining = cron_env["data_file"].read_text()
    assert other in remaining
    assert f"# pycastle:{cron_env['project_dir']}" not in remaining


def test_uninstall_does_not_touch_repo_whose_path_extends_ours(cron_env):
    """Marker match must anchor to end-of-line: '# pycastle:/a' must not match '# pycastle:/ab'."""
    sibling = f"{cron_env['project_dir']}-sibling"
    sibling_line = f"0 1 * * * {sibling}/pycastle/cron.sh # pycastle:{sibling}"
    cron_env["data_file"].write_text(sibling_line + "\n")

    _run(cron_env["install_sh"], cron_env["env"])
    _run(cron_env["uninstall_sh"], cron_env["env"])

    remaining = cron_env["data_file"].read_text()
    assert sibling_line in remaining


# ── Error handling ────────────────────────────────────────────────────────────


def test_install_fails_when_crontab_not_on_path(tmp_path):
    pycastle_dir = tmp_path / "pycastle"
    pycastle_dir.mkdir()
    install_sh = pycastle_dir / "cron-install.sh"
    install_sh.write_bytes((DEFAULTS_DIR / "cron-install.sh").read_bytes())
    install_sh.chmod(install_sh.stat().st_mode | stat.S_IEXEC)

    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()
    env = os.environ.copy()
    env["PATH"] = str(empty_bin)

    result = _run(install_sh, env)

    assert result.returncode != 0
    assert "crontab" in result.stderr.lower()


def test_uninstall_fails_when_crontab_not_on_path(tmp_path):
    pycastle_dir = tmp_path / "pycastle"
    pycastle_dir.mkdir()
    uninstall_sh = pycastle_dir / "cron-uninstall.sh"
    uninstall_sh.write_bytes((DEFAULTS_DIR / "cron-uninstall.sh").read_bytes())
    uninstall_sh.chmod(uninstall_sh.stat().st_mode | stat.S_IEXEC)

    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()
    env = os.environ.copy()
    env["PATH"] = str(empty_bin)

    result = _run(uninstall_sh, env)

    assert result.returncode != 0
    assert "crontab" in result.stderr.lower()
