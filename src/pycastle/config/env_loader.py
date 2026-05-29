from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

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
    global_dir: Path | None,
    local_env_file: Path,
    process_env: Mapping[str, str],
) -> dict[str, str]:
    del local_env_file
    merged: dict[str, str] = {}

    if global_dir is not None:
        merged.update(_read_env_file(global_dir / ".env"))

    merged.update(_read_env_file(DEFAULT_ENV_FILE))
    merged.update(process_env)
    return merged


def load_credential_env(
    global_dir: Path | None,
    local_env_file: Path,
    process_env: Mapping[str, str],
) -> dict[str, str]:
    resolved = load_env(
        global_dir=global_dir,
        local_env_file=local_env_file,
        process_env=process_env,
    )
    return {
        key: value for key in KNOWN_CREDENTIAL_ENV_KEYS if (value := resolved.get(key))
    }
