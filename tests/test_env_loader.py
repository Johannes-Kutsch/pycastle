from __future__ import annotations

from pathlib import Path

import pytest

from pycastle.config import (
    DEFAULT_ENV_FILE,
    KNOWN_CREDENTIAL_ENV_KEYS,
    load_credential_env,
    load_env,
)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pycastle").mkdir()
    return tmp_path


def test_local_env_resolves(repo: Path, tmp_path: Path) -> None:
    (repo / "pycastle" / ".env").write_text("GH_TOKEN=local\n")
    global_dir = tmp_path / "home"
    global_dir.mkdir()
    env = load_env(
        global_dir=global_dir,
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


def test_stale_local_env_path_does_not_relocate_fixed_local_env_layer(
    repo: Path, tmp_path: Path
) -> None:
    global_dir = tmp_path / "home"
    global_dir.mkdir()
    (global_dir / ".env").write_text("GH_TOKEN=fromglobal\nOTHER=fromglobal\n")
    (repo / "pycastle" / ".env").write_text("GH_TOKEN=fromlocal\n")
    custom = repo / "secrets.env"
    custom.write_text("GH_TOKEN=fromstale\nOTHER=fromstale\n")

    env = load_env(
        global_dir=global_dir,
        local_env_file=custom,
        process_env={},
    )

    assert env["GH_TOKEN"] == "fromlocal"
    assert env["OTHER"] == "fromglobal"


def test_load_env_uses_explicit_repo_root_when_cwd_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "workspace"
    repo_root.mkdir()
    (repo_root / "pycastle").mkdir()
    (repo_root / "pycastle" / ".env").write_text("GH_TOKEN=from-local\n")
    monkeypatch.chdir(tmp_path)

    env = load_env(
        global_dir=tmp_path / "no-global",
        local_env_file=DEFAULT_ENV_FILE,
        process_env={},
        repo_root=repo_root,
    )

    assert env["GH_TOKEN"] == "from-local"


# ── load_credential_env tests ──────────────────────────────────────────────────


def test_credential_env_excludes_unrelated_process_env_variables(
    repo: Path, tmp_path: Path
) -> None:
    (repo / "pycastle" / ".env").write_text("GH_TOKEN=tok\n")
    global_dir = tmp_path / "home"
    global_dir.mkdir()
    env = load_credential_env(
        global_dir=global_dir,
        local_env_file=DEFAULT_ENV_FILE,
        process_env={
            "GH_TOKEN": "tok",
            "PATH": "/usr/bin",
            "SHELL": "/bin/bash",
            "VIRTUAL_ENV": "/venv",
        },
    )
    assert set(env.keys()).issubset(set(KNOWN_CREDENTIAL_ENV_KEYS))
    assert "PATH" not in env
    assert "SHELL" not in env
    assert "VIRTUAL_ENV" not in env


def test_credential_env_contains_only_known_credential_keys(
    repo: Path, tmp_path: Path
) -> None:
    global_dir = tmp_path / "home"
    global_dir.mkdir()
    (global_dir / ".env").write_text(
        "GH_TOKEN=gh\nCLAUDE_CODE_OAUTH_TOKEN=claude\nCLAUDE_CODE_OAUTH_TOKEN_SECONDARY=sec\nOPENCODE_GO_API_KEY=oc\n"
    )
    env = load_credential_env(
        global_dir=global_dir,
        local_env_file=DEFAULT_ENV_FILE,
        process_env={},
    )
    assert set(env.keys()) == {
        "GH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY",
        "OPENCODE_GO_API_KEY",
    }


def test_credential_env_honours_process_env_wins_precedence(
    repo: Path, tmp_path: Path
) -> None:
    global_dir = tmp_path / "home"
    global_dir.mkdir()
    (global_dir / ".env").write_text("GH_TOKEN=fromglobal\n")
    (repo / "pycastle" / ".env").write_text("GH_TOKEN=fromlocal\n")
    env = load_credential_env(
        global_dir=global_dir,
        local_env_file=DEFAULT_ENV_FILE,
        process_env={"GH_TOKEN": "fromprocess"},
    )
    assert env["GH_TOKEN"] == "fromprocess"


def test_credential_env_returns_empty_when_no_credential_keys_present(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback_home = tmp_path / "xdg"
    fallback_pycastle_home = fallback_home / "pycastle"
    fallback_pycastle_home.mkdir(parents=True)
    (fallback_pycastle_home / ".env").write_text("GH_TOKEN=fromglobal\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fallback_home))
    global_dir = tmp_path / "isolated-home"
    global_dir.mkdir()

    env = load_credential_env(
        global_dir=global_dir,
        local_env_file=DEFAULT_ENV_FILE,
        process_env={"PATH": "/usr/bin", "HOME": "/home/user"},
    )
    assert env == {}
