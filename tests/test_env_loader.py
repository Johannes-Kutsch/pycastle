from __future__ import annotations

from pathlib import Path

import pytest

from pycastle.config import DEFAULT_ENV_FILE, load_env, resolve_pycastle_home


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pycastle").mkdir()
    return tmp_path


def test_local_env_resolves(repo: Path) -> None:
    (repo / "pycastle" / ".env").write_text("GH_TOKEN=local\n")
    env = load_env(
        global_dir=None,
        local_env_file=DEFAULT_ENV_FILE,
        process_env={},
    )
    assert env["GH_TOKEN"] == "local"


def test_process_env_overrides_file(repo: Path, tmp_path: Path) -> None:
    (repo / "pycastle" / ".env").write_text("GH_TOKEN=fromfile\n")
    global_dir = tmp_path / "home"
    global_dir.mkdir()
    (global_dir / ".env").write_text("GH_TOKEN=fromglobal\n")
    env = load_env(
        global_dir=global_dir,
        local_env_file=DEFAULT_ENV_FILE,
        process_env={"GH_TOKEN": "fromprocess"},
    )
    assert env["GH_TOKEN"] == "fromprocess"


def test_global_only_key_propagates_when_local_absent(
    repo: Path, tmp_path: Path
) -> None:
    global_dir = tmp_path / "home"
    global_dir.mkdir()
    (global_dir / ".env").write_text("GH_TOKEN=fromglobal\n")
    env = load_env(
        global_dir=global_dir,
        local_env_file=DEFAULT_ENV_FILE,
        process_env={},
    )
    assert env["GH_TOKEN"] == "fromglobal"


def test_local_overrides_global_per_key(repo: Path, tmp_path: Path) -> None:
    global_dir = tmp_path / "home"
    global_dir.mkdir()
    (global_dir / ".env").write_text(
        "GH_TOKEN=fromglobal\nCLAUDE_CODE_OAUTH_TOKEN=globalclaude\n"
    )
    (repo / "pycastle" / ".env").write_text("GH_TOKEN=fromlocal\n")
    env = load_env(
        global_dir=global_dir,
        local_env_file=DEFAULT_ENV_FILE,
        process_env={},
    )
    assert env["GH_TOKEN"] == "fromlocal"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "globalclaude"


def test_both_files_absent_returns_only_process_env(repo: Path, tmp_path: Path) -> None:
    global_dir = tmp_path / "home"
    global_dir.mkdir()
    env = load_env(
        global_dir=global_dir,
        local_env_file=DEFAULT_ENV_FILE,
        process_env={"GH_TOKEN": "p"},
    )
    assert env == {"GH_TOKEN": "p"}


def test_custom_env_file_skips_global_fallback(repo: Path, tmp_path: Path) -> None:
    global_dir = tmp_path / "home"
    global_dir.mkdir()
    (global_dir / ".env").write_text("GH_TOKEN=fromglobal\n")
    custom = repo / "secrets.env"
    custom.write_text("OTHER=x\n")
    env = load_env(
        global_dir=global_dir,
        local_env_file=custom,
        process_env={},
    )
    assert "GH_TOKEN" not in env
    assert env["OTHER"] == "x"


def test_resolve_pycastle_home_explicit_arg_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "from_env"))
    explicit = tmp_path / "explicit"
    assert resolve_pycastle_home(explicit) == explicit


def test_resolve_pycastle_home_falls_back_to_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "from_env"))
    assert resolve_pycastle_home() == tmp_path / "from_env"


def test_resolve_pycastle_home_returns_none_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PYCASTLE_HOME", raising=False)
    assert resolve_pycastle_home() is None
