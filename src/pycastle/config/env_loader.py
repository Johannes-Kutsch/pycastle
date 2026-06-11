from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values
from pycastle.layout import resolve_layout

__all__ = [
    "DEFAULT_ENV_FILE",
    "KNOWN_CREDENTIAL_ENV_KEYS",
    "load_credential_env",
    "load_env",
]

DEFAULT_ENV_FILE = Path("pycastle/.env")
KNOWN_CREDENTIAL_ENV_KEYS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY",
    "GH_TOKEN",
    "OPENCODE_GO_API_KEY",
)


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def load_env(
    global_dir: Path | None = None,
    local_env_file: Path = DEFAULT_ENV_FILE,
    process_env: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
) -> dict[str, str]:
    del local_env_file
    resolved_process_env = os.environ if process_env is None else process_env
    layout = resolve_layout(repo_root=repo_root, pycastle_home=global_dir)
    merged: dict[str, str] = {}

    merged.update(_read_env_file(layout.global_env_file))
    merged.update(_read_env_file(layout.local_env_file))
    merged.update(resolved_process_env)
    return merged


def load_credential_env(
    global_dir: Path | None = None,
    local_env_file: Path = DEFAULT_ENV_FILE,
    process_env: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
) -> dict[str, str]:
    resolved = load_env(
        global_dir=global_dir,
        local_env_file=local_env_file,
        process_env=process_env,
        repo_root=repo_root,
    )
    return {
        key: value for key in KNOWN_CREDENTIAL_ENV_KEYS if (value := resolved.get(key))
    }
