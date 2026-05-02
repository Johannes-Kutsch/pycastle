from __future__ import annotations

import shutil
from functools import lru_cache

from ..errors import ClaudeCliNotFoundError

# claude CLI does not expose a list-models subcommand; this list is kept in sync manually.
_KNOWN_MODELS: tuple[str, ...] = (
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-opus-4-7",
)


@lru_cache(maxsize=1)
def _list_models() -> tuple[str, ...]:
    """Return known Claude model IDs, verifying the CLI is installed. Cached for the process lifetime."""
    if shutil.which("claude") is None:
        raise ClaudeCliNotFoundError(
            "claude CLI not found; ensure it is installed and on PATH"
        )
    return _KNOWN_MODELS


class ClaudeService:
    def list_models(self) -> tuple[str, ...]:
        return _list_models()
