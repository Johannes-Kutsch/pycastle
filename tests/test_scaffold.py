import os
import stat
from pathlib import Path

import pytest

from pycastle.scaffold import (
    MANAGED_SCAFFOLD_ALLOWLIST,
    InitScaffold,
)


@pytest.fixture
def bundled_defaults(tmp_path: Path) -> Path:
    defaults = tmp_path / "defaults"
    setup_dir = defaults / "setup"
    setup_dir.mkdir(parents=True)
    (defaults / "config.py").write_text(
        "from pathlib import Path\n"
        "from pycastle import StageOverride\n\n"
        "# --- Behaviour ---\n"
        "# max_iterations = 10\n"
        '# bug_label = "bug"\n'
        "# auto_file_bugs = True\n"
        '# bug_report_repo = "owner/repo"\n\n'
        "# --- Logging ---\n"
        '# logs_dir = Path("pycastle/logs")\n\n'
        "# --- Stage overrides ---\n"
        '# plan_override = StageOverride(service="opencode", model="kimi-k2.6", effort="medium")\n'
        "# opencode_implement_override = StageOverride(\n"
        '#     service="opencode",\n'
        '#     model="kimi-k2.6",\n'
        '#     effort="medium",\n'
        '#     fallback=StageOverride(service="codex", model="gpt-5.4", effort="medium"),\n'
        "# )\n"
        "implement_override = StageOverride(\n"
        '    service="codex",\n'
        '    model="gpt-5.4",\n'
        '    effort="medium",\n'
        ")\n"
    )
    (defaults / ".gitignore").write_text(".env\nsetup/\n")
    (setup_dir / "cron.sh").write_text("#!/bin/sh\necho cron\n")
    (setup_dir / "cron-install.sh").write_text("#!/bin/sh\necho install\n")
    (setup_dir / "cron-uninstall.sh").write_text("#!/bin/sh\necho uninstall\n")
    return defaults


@pytest.fixture
def init_scaffold(tmp_path: Path, bundled_defaults: Path) -> InitScaffold:
    return InitScaffold(
        pycastle_dir=tmp_path / "pycastle",
        pycastle_home=tmp_path / "home",
        defaults=bundled_defaults,
    )


def test_managed_scaffold_allowlist_is_explicit():
    assert MANAGED_SCAFFOLD_ALLOWLIST == frozenset(
        {
            ".gitignore",
            "setup/cron.sh",
            "setup/cron-install.sh",
            "setup/cron-uninstall.sh",
        }
    )


def test_init_scaffold_refresh_reports_managed_scaffold_allowlist_statuses(
    init_scaffold: InitScaffold,
):
    report = init_scaffold.refresh()

    assert [(entry.status, entry.path) for entry in report] == [
        ("created", "config.py.example"),
        ("created", ".gitignore"),
        ("created", "setup/cron-install.sh"),
        ("created", "setup/cron-uninstall.sh"),
        ("created", "setup/cron.sh"),
    ]


def test_init_scaffold_refresh_reports_overwrote_unchanged_and_preserved_statuses(
    init_scaffold: InitScaffold,
):
    pycastle_dir = init_scaffold.pycastle_dir
    pycastle_dir.mkdir(parents=True)
    (pycastle_dir / "config.py.example").write_text("stale example\n")
    (pycastle_dir / ".gitignore").write_text(".env\nsetup/\n")
    (pycastle_dir / "setup").mkdir()
    (pycastle_dir / "setup" / "cron.sh").write_text("stale cron\n")
    (pycastle_dir / "setup" / "cron-install.sh").write_text("#!/bin/sh\necho install\n")
    (pycastle_dir / "setup" / "cron-uninstall.sh").write_text(
        "#!/bin/sh\necho uninstall\n"
    )
    (pycastle_dir / "config.py").write_text("# user-owned config\n")
    (pycastle_dir / ".env").write_text("GH_TOKEN=secret\n")
    (pycastle_dir / "prompts").mkdir()
    (pycastle_dir / "prompts" / "plan.md").write_text("user override\n")

    report = init_scaffold.refresh()

    assert [(entry.status, entry.path) for entry in report] == [
        ("overwrote", "config.py.example"),
        ("unchanged", ".gitignore"),
        ("unchanged", "setup/cron-install.sh"),
        ("unchanged", "setup/cron-uninstall.sh"),
        ("overwrote", "setup/cron.sh"),
        ("preserved", "config.py"),
        ("preserved", ".env"),
    ]
    assert (pycastle_dir / "config.py").read_text() == "# user-owned config\n"
    assert (pycastle_dir / ".env").read_text() == "GH_TOKEN=secret\n"
    assert (pycastle_dir / "prompts" / "plan.md").read_text() == "user override\n"


def test_init_scaffold_refresh_preserves_non_managed_artifacts_without_reporting_them(
    init_scaffold: InitScaffold,
):
    pycastle_dir = init_scaffold.pycastle_dir
    (pycastle_dir / "prompts").mkdir(parents=True)
    (pycastle_dir / "prompts" / "coordination.md").write_text("prompt override\n")
    (pycastle_dir / "Dockerfile").write_text("FROM user-owned\n")
    (pycastle_dir / "__pycache__").mkdir()
    (pycastle_dir / "__pycache__" / "config.cpython-313.pyc").write_bytes(
        b"\0pyc cache"
    )

    report = init_scaffold.refresh()

    assert [(entry.status, entry.path) for entry in report] == [
        ("created", "config.py.example"),
        ("created", ".gitignore"),
        ("created", "setup/cron-install.sh"),
        ("created", "setup/cron-uninstall.sh"),
        ("created", "setup/cron.sh"),
    ]
    assert (
        pycastle_dir / "prompts" / "coordination.md"
    ).read_text() == "prompt override\n"
    assert (pycastle_dir / "Dockerfile").read_text() == "FROM user-owned\n"
    assert (
        pycastle_dir / "__pycache__" / "config.cpython-313.pyc"
    ).read_bytes() == b"\0pyc cache"


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX executable bit not meaningful on Windows"
)
def test_init_scaffold_refresh_marks_setup_scaffold_scripts_executable(
    init_scaffold: InitScaffold,
):
    pycastle_dir = init_scaffold.pycastle_dir

    init_scaffold.refresh()

    for rel_path in (
        "setup/cron.sh",
        "setup/cron-install.sh",
        "setup/cron-uninstall.sh",
    ):
        mode = (pycastle_dir / rel_path).stat().st_mode
        assert mode & stat.S_IXUSR


def test_init_scaffold_install_defaults_refreshes_existing_global_config_example(
    init_scaffold: InitScaffold,
):
    init_scaffold.pycastle_home.mkdir(parents=True)
    (init_scaffold.pycastle_home / "config.py.example").write_text("stale global\n")

    init_scaffold.install_defaults()

    assert (
        init_scaffold.pycastle_dir / "config.py.example"
    ).read_text() == init_scaffold.render_config_example()
    assert (
        init_scaffold.pycastle_home / "config.py.example"
    ).read_text() == init_scaffold.render_config_example()


def test_init_scaffold_install_defaults_does_not_create_global_config_example_when_absent(
    init_scaffold: InitScaffold,
):
    init_scaffold.install_defaults()

    assert (
        init_scaffold.pycastle_dir / "config.py.example"
    ).read_text() == init_scaffold.render_config_example()
    assert not (init_scaffold.pycastle_home / "config.py.example").exists()


def test_init_scaffold_refresh_renders_local_config_example_from_bundled_defaults(
    init_scaffold: InitScaffold,
):
    config_example = init_scaffold.pycastle_dir / "config.py.example"
    config_example.parent.mkdir(parents=True)
    config_example.write_text("stale example\n")

    init_scaffold.refresh()

    assert config_example.read_text() == (
        "from pathlib import Path\n\n"
        "from pycastle import StageOverride\n\n"
        "# --- Behaviour ---\n"
        "max_iterations = 10\n"
        'bug_label = "bug"\n\n'
        "# --- Logging ---\n"
        'logs_dir = Path("pycastle/logs")\n\n'
        "# --- Stage overrides ---\n"
        '# plan_override = StageOverride(service="opencode", model="kimi-k2.6", effort="medium")\n'
        "# opencode_implement_override = StageOverride(\n"
        '#     service="opencode",\n'
        '#     model="kimi-k2.6",\n'
        '#     effort="medium",\n'
        '#     fallback=StageOverride(service="codex", model="gpt-5.4", effort="medium"),\n'
        "# )\n"
        "implement_override = StageOverride(\n"
        '    service="codex",\n'
        '    model="gpt-5.4",\n'
        '    effort="medium",\n'
        ")\n"
    )


def test_init_scaffold_refresh_treats_crlf_config_example_as_unchanged(
    init_scaffold: InitScaffold,
):
    config_example = init_scaffold.pycastle_dir / "config.py.example"
    config_example.parent.mkdir(parents=True)
    config_example.write_bytes(
        init_scaffold.render_config_example().replace("\n", "\r\n").encode()
    )

    report = init_scaffold.refresh()

    assert (report[0].status, report[0].path) == ("unchanged", "config.py.example")


def test_init_scaffold_refresh_reports_existing_pycastle_home_config_example_status(
    init_scaffold: InitScaffold,
):
    init_scaffold.pycastle_home.mkdir(parents=True)
    (init_scaffold.pycastle_home / "config.py.example").write_text("stale global\n")

    report = init_scaffold.refresh()

    assert [(entry.status, entry.path) for entry in report[:2]] == [
        ("created", "config.py.example"),
        ("overwrote", "pycastle home/config.py.example"),
    ]
