from __future__ import annotations

from pathlib import Path

import pytest

from pycastle.config import (
    DEFAULT_ENV_FILE,
    KNOWN_CREDENTIAL_ENV_KEYS,
    parse_credential_list,
    load_config,
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


def test_load_config_and_credential_env_share_explicit_path_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "workspace"
    repo_root.mkdir()
    pycastle_dir = repo_root / "pycastle"
    pycastle_dir.mkdir()
    (pycastle_dir / "config.py").write_text("max_parallel = 7\n")
    (pycastle_dir / ".env").write_text("GH_TOKEN=from-local\n")

    global_dir = tmp_path / "pycastle-home"
    global_dir.mkdir()
    (global_dir / "config.py").write_text("max_parallel = 5\n")
    (global_dir / ".env").write_text("CLAUDE_CODE_OAUTH_TOKEN=from-global\n")

    monkeypatch.chdir(tmp_path)

    cfg = load_config(repo_root=repo_root, global_dir=global_dir)
    env = load_credential_env(
        repo_root=repo_root,
        global_dir=global_dir,
        local_env_file=DEFAULT_ENV_FILE,
        process_env={},
    )

    assert cfg.max_parallel == 7
    assert env == {
        "CLAUDE_CODE_OAUTH_TOKEN": "from-global",
        "GH_TOKEN": "from-local",
    }


def test_load_config_and_credential_env_share_pycastle_home_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "workspace"
    repo_root.mkdir()
    (repo_root / "pycastle").mkdir()

    pycastle_home = tmp_path / "pycastle-home"
    pycastle_home.mkdir()
    (pycastle_home / "config.py").write_text("max_parallel = 6\n")
    (pycastle_home / ".env").write_text("GH_TOKEN=from-global\n")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(pycastle_home))

    cfg = load_config(repo_root=repo_root)
    env = load_credential_env(
        repo_root=repo_root,
        local_env_file=DEFAULT_ENV_FILE,
        process_env={},
    )

    assert cfg.max_parallel == 6
    assert env == {"GH_TOKEN": "from-global"}


def test_load_config_and_credential_env_share_process_env_pycastle_home_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "workspace"
    repo_root.mkdir()
    (repo_root / "pycastle").mkdir()

    pycastle_home = tmp_path / "pycastle-home"
    pycastle_home.mkdir()
    (pycastle_home / "config.py").write_text("max_parallel = 8\n")
    (pycastle_home / ".env").write_text("GH_TOKEN=from-global\n")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(pycastle_home))

    cfg = load_config(repo_root=repo_root)
    monkeypatch.delenv("PYCASTLE_HOME")
    env = load_credential_env(
        repo_root=repo_root,
        local_env_file=DEFAULT_ENV_FILE,
        process_env={"PYCASTLE_HOME": str(pycastle_home)},
    )

    assert cfg.max_parallel == 8
    assert env == {"GH_TOKEN": "from-global"}


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


def test_credential_env_includes_numbered_credentials(
    repo: Path, tmp_path: Path
) -> None:
    global_dir = tmp_path / "home"
    global_dir.mkdir()
    (global_dir / ".env").write_text(
        "GH_TOKEN=gh\n"
        "CLAUDE_CODE_OAUTH_TOKEN=primary\n"
        "CLAUDE_CODE_OAUTH_TOKEN_2=secondary\n"
        "CLAUDE_CODE_OAUTH_TOKEN_7=slot7\n"
        "OPENCODE_GO_API_KEY=primary-opencode\n"
        "OPENCODE_GO_API_KEY_2=secondary-opencode\n"
        "OPENCODE_GO_API_KEY_12=slot12\n"
    )
    env = load_credential_env(
        global_dir=global_dir,
        local_env_file=DEFAULT_ENV_FILE,
        process_env={},
    )
    assert env["CLAUDE_CODE_OAUTH_TOKEN_2"] == "secondary"
    assert env["CLAUDE_CODE_OAUTH_TOKEN_7"] == "slot7"
    assert env["OPENCODE_GO_API_KEY_2"] == "secondary-opencode"
    assert env["OPENCODE_GO_API_KEY_12"] == "slot12"


def test_credential_env_excludes_non_base_key_numbered_suffixes(
    repo: Path, tmp_path: Path
) -> None:
    global_dir = tmp_path / "home"
    global_dir.mkdir()
    (global_dir / ".env").write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=primary\n"
        "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY_2=legacy-numbered\n"
        "OPENCODE_GO_API_KEY_EXTRA_2=not-a-credential\n"
    )
    env = load_credential_env(
        global_dir=global_dir,
        local_env_file=DEFAULT_ENV_FILE,
        process_env={},
    )
    assert env == {"CLAUDE_CODE_OAUTH_TOKEN": "primary"}


def test_parse_credential_list_orders_slotted_credentials_for_a_service() -> None:
    credential_env = {
        "CLAUDE_CODE_OAUTH_TOKEN_10": "slot10",
        "CLAUDE_CODE_OAUTH_TOKEN_3": "slot3",
        "CLAUDE_CODE_OAUTH_TOKEN": "slot1",
        "CLAUDE_CODE_OAUTH_TOKEN_2": "slot2",
    }
    assert parse_credential_list(credential_env, "CLAUDE_CODE_OAUTH_TOKEN") == [
        (1, "slot1"),
        (2, "slot2"),
        (3, "slot3"),
        (10, "slot10"),
    ]


def test_parse_credential_list_ignores_legacy_secondary_key() -> None:
    credential_env = {
        "CLAUDE_CODE_OAUTH_TOKEN": "primary",
        "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY": "secondary",
    }
    assert parse_credential_list(credential_env, "CLAUDE_CODE_OAUTH_TOKEN") == [
        (1, "primary")
    ]


def test_parse_credential_list_rejects_bare_and_slot_1_both_set() -> None:
    with pytest.raises(
        ValueError,
        match=r"CLAUDE_CODE_OAUTH_TOKEN and CLAUDE_CODE_OAUTH_TOKEN_1",
    ):
        parse_credential_list(
            {
                "CLAUDE_CODE_OAUTH_TOKEN": "primary",
                "CLAUDE_CODE_OAUTH_TOKEN_1": "secondary",
            },
            "CLAUDE_CODE_OAUTH_TOKEN",
        )


def test_parse_credential_list_with_only_primary_credential_is_single_item() -> None:
    assert parse_credential_list(
        {"OPENCODE_GO_API_KEY": "one"},
        "OPENCODE_GO_API_KEY",
    ) == [(1, "one")]


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
