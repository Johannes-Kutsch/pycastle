from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values
from pycastle.layout import resolve_layout

__all__ = [
    "DEFAULT_ENV_FILE",
    "KNOWN_CREDENTIAL_ENV_KEYS",
    "parse_credential_list",
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
_NUMBERED_SUFFIX_RE = re.compile(r"^(.+)_([0-9]+)$")
_CLAUDE_PREFIX = "CLAUDE_CODE_OAUTH_TOKEN"
_OPENCODE_PREFIX = "OPENCODE_GO_API_KEY"


def _is_credential_env_key(key: str) -> bool:
    if key in KNOWN_CREDENTIAL_ENV_KEYS:
        return True
    if not key.startswith(_CLAUDE_PREFIX) and not key.startswith(_OPENCODE_PREFIX):
        return False
    return _NUMBERED_SUFFIX_RE.match(key) is not None


def parse_credential_list(
    credential_env: Mapping[str, str], base_key: str
) -> list[tuple[int, str]]:
    """Parse one service's credential keys into an ordered (slot, value) list."""
    values: dict[int, str] = {}
    bare_value = credential_env.get(base_key)
    slot1_key = f"{base_key}_1"
    slot1_value = credential_env.get(slot1_key)
    if bare_value is not None and slot1_value is not None:
        raise ValueError(
            f"cannot resolve slot 1 for {base_key}: both {base_key} and {slot1_key} are set"
        )

    if bare_value is not None:
        values[1] = bare_value
    elif slot1_value is not None:
        values[1] = slot1_value

    if bare_value is None and slot1_value is None:
        slot_pattern = re.compile(rf"^{re.escape(base_key)}_(?P<slot>[0-9]+)$")
    else:
        slot_pattern = re.compile(rf"^{re.escape(base_key)}_(?P<slot>[2-9][0-9]*)$")

    for key, value in credential_env.items():
        match = slot_pattern.match(key)
        if not match:
            continue
        slot = int(match.group("slot"))
        values[slot] = value

    return [(slot, values[slot]) for slot in sorted(values)]


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def _resolve_env_files(
    global_dir: Path | None,
    repo_root: Path | None,
    env: Mapping[str, str],
) -> tuple[Path, Path]:
    layout = resolve_layout(repo_root=repo_root, pycastle_home=global_dir, env=env)
    return layout.global_env_file, layout.local_env_file


def load_env(
    global_dir: Path | None = None,
    local_env_file: Path = DEFAULT_ENV_FILE,
    process_env: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
) -> dict[str, str]:
    del local_env_file
    resolved_process_env = os.environ if process_env is None else process_env
    layout_env = os.environ if process_env is None else {**os.environ, **process_env}
    global_env_file, local_env_file = _resolve_env_files(
        global_dir, repo_root, layout_env
    )
    merged: dict[str, str] = {}

    merged.update(_read_env_file(global_env_file))
    merged.update(_read_env_file(local_env_file))
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
        key: value
        for key in resolved
        if _is_credential_env_key(key) and (value := resolved.get(key))
    }
