from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

__all__ = ["DEFAULT_ENV_FILE", "load_env"]

DEFAULT_ENV_FILE = Path("pycastle/.env")


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def load_env(
    global_dir: Path | None,
    local_env_file: Path,
    process_env: Mapping[str, str],
) -> dict[str, str]:
    merged: dict[str, str] = {}

    if local_env_file == DEFAULT_ENV_FILE and global_dir is not None:
        merged.update(_read_env_file(global_dir / ".env"))

    merged.update(_read_env_file(local_env_file))
    merged.update(process_env)
    return merged
