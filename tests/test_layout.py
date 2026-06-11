from __future__ import annotations

from pathlib import Path

from pycastle.layout import describe_config_layers, resolve_global_dir, resolve_layout


def test_resolve_layout_returns_fixed_pycastle_paths_and_cron_lock_path(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "workspace"
    pycastle_home = tmp_path / "pycastle-home"

    layout = resolve_layout(repo_root=repo_root, pycastle_home=pycastle_home)

    assert layout.repo_root == repo_root
    assert layout.pycastle_dir == repo_root / "pycastle"
    assert layout.pycastle_home == pycastle_home
    assert layout.global_config_file == pycastle_home / "config.py"
    assert layout.local_config_file == repo_root / "pycastle" / "config.py"
    assert layout.global_env_file == pycastle_home / ".env"
    assert layout.local_env_file == repo_root / "pycastle" / ".env"
    assert layout.cron_lock_path == pycastle_home / ".cron.lock"


def test_resolve_layout_pycastle_home_precedence_prefers_explicit_over_env(
    tmp_path: Path,
) -> None:
    explicit_pycastle_home = tmp_path / "explicit"
    env_pycastle_home = tmp_path / "from-env"

    layout = resolve_layout(
        repo_root=tmp_path,
        pycastle_home=explicit_pycastle_home,
        env={"PYCASTLE_HOME": str(env_pycastle_home)},
    )

    assert layout.pycastle_home == explicit_pycastle_home


def test_resolve_layout_pycastle_home_precedence_falls_back_to_env(
    tmp_path: Path,
) -> None:
    env_pycastle_home = tmp_path / "from-env"

    layout = resolve_layout(
        repo_root=tmp_path,
        env={"PYCASTLE_HOME": str(env_pycastle_home)},
    )

    assert layout.pycastle_home == env_pycastle_home


def test_layout_describe_config_layers_shortens_pycastle_home_to_tilde(
    tmp_path: Path, monkeypatch
) -> None:
    fake_home = tmp_path / "home"
    global_dir = fake_home / ".config" / "pycastle"
    global_dir.mkdir(parents=True)
    (global_dir / "config.py").write_text("")
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)

    summary = describe_config_layers(repo_root=tmp_path, global_dir=global_dir)

    assert summary == "Config: defaults + ~/.config/pycastle/config.py"


def test_layout_describe_config_layers_uses_appdata_form_on_windows(
    tmp_path: Path, monkeypatch
) -> None:
    appdata = tmp_path / "appdata"
    global_dir = appdata / "pycastle"
    global_dir.mkdir(parents=True)
    (global_dir / "config.py").write_text("")
    monkeypatch.setenv("APPDATA", str(appdata))

    summary = describe_config_layers(
        repo_root=tmp_path,
        global_dir=global_dir,
        os_name="nt",
    )

    assert summary == r"Config: defaults + %APPDATA%\pycastle\config.py"


def test_resolve_global_dir_prefers_explicit_arg_over_env(
    tmp_path: Path,
) -> None:
    explicit_pycastle_home = tmp_path / "explicit"

    assert (
        resolve_global_dir(
            explicit_pycastle_home, {"PYCASTLE_HOME": str(tmp_path / "ignored")}
        )
        == explicit_pycastle_home
    )
