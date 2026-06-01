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
    report = init_scaffold.refresh(config_example_text="example = 1\n")

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

    report = init_scaffold.refresh(config_example_text="example = 1\n")

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


def test_init_scaffold_install_defaults_refreshes_existing_global_config_example(
    init_scaffold: InitScaffold,
):
    init_scaffold.pycastle_home.mkdir(parents=True)
    (init_scaffold.pycastle_home / "config.py.example").write_text("stale global\n")

    init_scaffold.install_defaults(config_example_text="example = 1\n")

    assert (
        init_scaffold.pycastle_dir / "config.py.example"
    ).read_text() == "example = 1\n"
    assert (
        init_scaffold.pycastle_home / "config.py.example"
    ).read_text() == "example = 1\n"


def test_init_scaffold_install_defaults_does_not_create_global_config_example_when_absent(
    init_scaffold: InitScaffold,
):
    init_scaffold.install_defaults(config_example_text="example = 1\n")

    assert (
        init_scaffold.pycastle_dir / "config.py.example"
    ).read_text() == "example = 1\n"
    assert not (init_scaffold.pycastle_home / "config.py.example").exists()
