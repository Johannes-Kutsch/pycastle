import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from pycastle.config.loader import derive_docker_image_name

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


def test_install_global_logs_dir_appends_sanitized_project_name(cron_env):
    global_dir = cron_env["project_dir"] / "global"
    global_dir.mkdir()
    (global_dir / "config.py").write_text(
        "from pathlib import Path\nlogs_dir = Path('shared-logs')\n"
    )
    cron_env["env"]["PYCASTLE_HOME"] = str(global_dir)

    result = _run(cron_env["install_sh"], cron_env["env"])

    assert result.returncode == 0, result.stderr
    expected_log = (
        cron_env["project_dir"]
        / "shared-logs"
        / derive_docker_image_name(cron_env["project_dir"].name)
        / "cron.log"
    )
    assert f">> {expected_log} 2>&1" in cron_env["data_file"].read_text()
    assert expected_log.parent.is_dir()


def test_install_local_logs_dir_entry_runs_through_effective_logs_dir_with_spaces(
    tmp_path, fake_crontab
):
    project_dir = tmp_path / "My Project"
    project_dir.mkdir()
    pycastle_dir = project_dir / "pycastle"
    pycastle_dir.mkdir()
    (pycastle_dir / "config.py").write_text(
        "from pathlib import Path\nlogs_dir = Path('custom logs')\n"
    )

    setup_dir = pycastle_dir / "setup"
    setup_dir.mkdir()
    install_sh = setup_dir / "cron-install.sh"
    install_sh.write_bytes((SETUP_DIR / "cron-install.sh").read_bytes())
    install_sh.chmod(
        install_sh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
    )

    cron_sh = setup_dir / "cron.sh"
    cron_sh.write_text("#!/usr/bin/env bash\necho cron tick\n")
    cron_sh.chmod(cron_sh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    bin_dir, data_file = fake_crontab
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    result = subprocess.run(
        [_BASH, str(install_sh)],
        cwd=project_dir,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    cron_line = data_file.read_text().strip()
    cron_command = cron_line.split(maxsplit=5)[5]

    run_result = subprocess.run(
        [_BASH, "-lc", cron_command],
        cwd=project_dir,
        env=env,
        capture_output=True,
        text=True,
    )

    expected_log = project_dir / "custom logs" / "cron.log"
    assert run_result.returncode == 0, run_result.stderr
    assert expected_log.read_text().strip() == "cron tick"


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


# ── cron.sh shared fixture ────────────────────────────────────────────────────


@pytest.fixture()
def cron_sh_env(tmp_path):
    """Fake project structure for cron.sh; provides pip and pycastle shims."""
    setup_dir = tmp_path / "pycastle" / "setup"
    setup_dir.mkdir(parents=True)

    cron_sh_dst = setup_dir / "cron.sh"
    cron_sh_dst.write_bytes((SETUP_DIR / "cron.sh").read_bytes())
    cron_sh_dst.chmod(
        cron_sh_dst.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
    )

    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)

    python_shim = venv_bin / "python"
    python_shim.write_text(
        "#!/usr/bin/env bash\n"
        'case "${1:-}" in\n'
        "    -m) exit 0 ;;\n"
        "    *) exit 0 ;;\n"
        "esac\n"
    )
    python_shim.chmod(
        python_shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
    )

    pycastle_shim = venv_bin / "pycastle"
    pycastle_shim.write_text("#!/usr/bin/env bash\nexit 0\n")
    pycastle_shim.chmod(
        pycastle_shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
    )

    env = os.environ.copy()

    return {
        "cron_sh": cron_sh_dst,
        "env": env,
        "venv_bin": venv_bin,
        "python_shim": python_shim,
        "pycastle_shim": pycastle_shim,
    }


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_cron_sh(cron_sh_env, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_BASH, str(cron_sh_env["cron_sh"]), *args],
        env=cron_sh_env["env"],
        capture_output=True,
        text=True,
    )


def _install_python_shim(cron_sh_env, pip_body: str) -> None:
    """Overwrite the python shim with custom behaviour for `python -m pip ...` calls.

    `pip_body` is the bash snippet executed inside the `-m)` case.
    """
    cron_sh_env["python_shim"].write_text(
        "#!/usr/bin/env bash\n"
        'case "${1:-}" in\n'
        f"    -m) {pip_body} ;;\n"
        "    *) exit 0 ;;\n"
        "esac\n"
    )
    _make_executable(cron_sh_env["python_shim"])


# ── cron.sh pip-upgrade best-effort ──────────────────────────────────────────


def test_pip_upgrade_failure_emits_two_warnings(cron_sh_env):
    """Each failing pip call must emit its own warning — two warnings total."""
    _install_python_shim(
        cron_sh_env, "echo 'pip upgrade failed (simulated)' >&2; exit 1"
    )

    result = _run_cron_sh(cron_sh_env)

    warnings = [
        line for line in result.stderr.splitlines() if "warning" in line.lower()
    ]
    assert len(warnings) >= 2, f"expected >=2 warning lines, got: {result.stderr!r}"


def test_pip_upgrade_failure_both_calls_are_independent(cron_sh_env, tmp_path):
    """Second pip call runs even when first fails (no branching between them)."""
    pip_calls_file = tmp_path / "pip_calls.txt"
    pip_calls_file.write_text("")

    _install_python_shim(
        cron_sh_env,
        (
            f'echo pip >> "{pip_calls_file}"; '
            f'if [ "$(wc -l < "{pip_calls_file}")" -eq 1 ]; then '
            "echo 'pip upgrade failed (simulated attempt 1)' >&2; exit 1; "
            "fi; exit 0"
        ),
    )

    result = _run_cron_sh(cron_sh_env)

    pip_calls = pip_calls_file.read_text().strip().splitlines()
    assert len(pip_calls) == 2, f"expected 2 pip calls, got {len(pip_calls)}"
    assert result.returncode == 0, result.stderr
    warnings = [
        line for line in result.stderr.splitlines() if "warning" in line.lower()
    ]
    assert len(warnings) == 1


def test_pip_upgrade_comment_records_rationale():
    """cron.sh must have an inline comment near the pip calls explaining warn-and-continue."""
    lines = (SETUP_DIR / "cron.sh").read_text().splitlines()
    pip_idx = next(
        i
        for i, line in enumerate(lines)
        if "pip install --upgrade" in line and not line.lstrip().startswith("#")
    )
    nearby = "\n".join(lines[max(0, pip_idx - 8) : pip_idx])
    assert any(
        token in nearby for token in ("stale", "prefer", "skipped", "last night")
    ), f"expected warn-and-continue rationale near pip calls; got:\n{nearby}"
