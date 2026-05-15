import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="bash/crontab not available on Windows"
)

DEFAULTS_DIR = Path(__file__).parent.parent / "src" / "pycastle" / "defaults"
SETUP_DIR = DEFAULTS_DIR / "setup"


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

    setup_dir = tmp_path / "pycastle" / "setup"
    setup_dir.mkdir(parents=True)

    for script in ["cron-install.sh", "cron-uninstall.sh"]:
        dst = setup_dir / script
        dst.write_bytes((SETUP_DIR / script).read_bytes())
        dst.chmod(dst.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    return {
        "project_dir": tmp_path,
        "install_sh": setup_dir / "cron-install.sh",
        "uninstall_sh": setup_dir / "cron-uninstall.sh",
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


def test_install_redirects_output_to_logs_dir(cron_env):
    """Crontab line must redirect stdout+stderr into <logs_dir>/cron.log."""
    result = _run(cron_env["install_sh"], cron_env["env"])

    assert result.returncode == 0, result.stderr
    content = cron_env["data_file"].read_text()
    expected_log = cron_env["project_dir"] / "pycastle" / "logs" / "cron.log"
    assert f">> {expected_log} 2>&1" in content


def test_install_creates_logs_dir(cron_env):
    """The logs directory must exist after install so cron can write to it."""
    _run(cron_env["install_sh"], cron_env["env"])

    assert (cron_env["project_dir"] / "pycastle" / "logs").is_dir()


def test_install_marker_remains_end_of_line(cron_env):
    """The marker must stay at end-of-line so cron-uninstall.sh can still match it."""
    _run(cron_env["install_sh"], cron_env["env"])

    content = cron_env["data_file"].read_text()
    marker = f"# pycastle:{cron_env['project_dir']}"
    lines = [line for line in content.splitlines() if marker in line]
    assert lines, "expected at least one line containing the marker"
    for line in lines:
        assert line.endswith(marker), f"marker not at end-of-line: {line!r}"


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
    other = "0 2 * * * /other/repo/pycastle/setup/cron.sh # pycastle:/other/repo"
    cron_env["data_file"].write_text(other + "\n")

    _run(cron_env["install_sh"], cron_env["env"])
    _run(cron_env["uninstall_sh"], cron_env["env"])

    remaining = cron_env["data_file"].read_text()
    assert other in remaining
    assert f"# pycastle:{cron_env['project_dir']}" not in remaining


def test_uninstall_does_not_touch_repo_whose_path_extends_ours(cron_env):
    """Marker match must anchor to end-of-line: '# pycastle:/a' must not match '# pycastle:/ab'."""
    sibling = f"{cron_env['project_dir']}-sibling"
    sibling_line = f"0 1 * * * {sibling}/pycastle/setup/cron.sh # pycastle:{sibling}"
    cron_env["data_file"].write_text(sibling_line + "\n")

    _run(cron_env["install_sh"], cron_env["env"])
    _run(cron_env["uninstall_sh"], cron_env["env"])

    remaining = cron_env["data_file"].read_text()
    assert sibling_line in remaining


# ── Error handling ────────────────────────────────────────────────────────────


def test_install_fails_when_crontab_not_on_path(tmp_path):
    setup_dir = tmp_path / "pycastle" / "setup"
    setup_dir.mkdir(parents=True)
    install_sh = setup_dir / "cron-install.sh"
    install_sh.write_bytes((SETUP_DIR / "cron-install.sh").read_bytes())
    install_sh.chmod(install_sh.stat().st_mode | stat.S_IEXEC)

    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()
    env = os.environ.copy()
    env["PATH"] = str(empty_bin)

    result = _run(install_sh, env)

    assert result.returncode != 0
    assert "crontab" in result.stderr.lower()


def test_uninstall_fails_when_crontab_not_on_path(tmp_path):
    setup_dir = tmp_path / "pycastle" / "setup"
    setup_dir.mkdir(parents=True)
    uninstall_sh = setup_dir / "cron-uninstall.sh"
    uninstall_sh.write_bytes((SETUP_DIR / "cron-uninstall.sh").read_bytes())
    uninstall_sh.chmod(uninstall_sh.stat().st_mode | stat.S_IEXEC)

    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()
    env = os.environ.copy()
    env["PATH"] = str(empty_bin)

    result = _run(uninstall_sh, env)

    assert result.returncode != 0
    assert "crontab" in result.stderr.lower()


# ── cron.sh contents ──────────────────────────────────────────────────────────


def test_cron_sh_does_not_install_consuming_project_deps():
    """cron.sh must not pip-install the consuming project on the host venv."""
    cron_sh = SETUP_DIR / "cron.sh"
    content = cron_sh.read_text()
    assert "pip install -e" not in content
    assert "pip install -r requirements.txt" not in content
