from __future__ import annotations

import subprocess
from functools import lru_cache

from .errors import (
    ClaudeCliNotFoundError,
    ClaudeCommandError,
    ClaudeServiceError,
    ClaudeTimeoutError,
)


@lru_cache(maxsize=1)
def _list_models() -> tuple[str, ...]:
    """Invoke `claude list-models` and return available model IDs. Cached for the process lifetime."""
    try:
        result = subprocess.run(
            ["claude", "list-models"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError as exc:
        raise ClaudeCliNotFoundError(
            "claude CLI not found; ensure it is installed and on PATH"
        ) from exc
    except OSError as exc:
        raise ClaudeServiceError(f"claude list-models OS error: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ClaudeTimeoutError("claude list-models timed out after 10 s") from exc

    if result.returncode != 0:
        raise ClaudeCommandError(
            f"claude list-models failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    models = tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
    if not models:
        raise ClaudeServiceError("claude list-models returned no models")
    return models


class ClaudeService:
    def list_models(self) -> tuple[str, ...]:
        return _list_models()
