from __future__ import annotations

import re
import subprocess
from difflib import get_close_matches
from functools import lru_cache

from .errors import ConfigValidationError

_VALID_EFFORTS = frozenset({"low", "normal", "high"})
_MODEL_RE = re.compile(r"^claude-(haiku|sonnet|opus)-(.+)$")


def _parse_version(version_str: str) -> tuple[int, ...]:
    parts = re.split(r"[-.]", version_str)
    return tuple(int(p) if p.isdigit() else 0 for p in parts)


@lru_cache(maxsize=1)
def _fetch_models() -> tuple[str, ...]:
    """Invoke `claude list-models` and return available model IDs. Cached for the process lifetime."""
    try:
        result = subprocess.run(
            ["claude", "list-models"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError as exc:
        raise ConfigValidationError(
            "claude CLI not found; ensure it is installed and on PATH",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ConfigValidationError(
            "claude list-models timed out after 10 s",
        ) from exc

    if result.returncode != 0:
        raise ConfigValidationError(
            f"claude list-models failed (exit {result.returncode}): {result.stderr.strip()}",
        )

    models = tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
    if not models:
        raise ConfigValidationError("claude list-models returned no models")
    return models


def _known_shorthands(models: tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for m in models:
        match = _MODEL_RE.match(m)
        if match:
            family = match.group(1)
            if family not in seen:
                seen.add(family)
                result.append(family)
    return sorted(result)


def _resolve_shorthand(shorthand: str, models: tuple[str, ...]) -> str:
    candidates = [
        (m, _parse_version(match.group(2)))
        for m in models
        if (match := _MODEL_RE.match(m)) and match.group(1) == shorthand
    ]

    if candidates:
        return max(candidates, key=lambda x: x[1])[0]

    valid = _known_shorthands(models)
    close = get_close_matches(shorthand, valid, n=1, cutoff=0.0)
    suggestion = close[0] if close else (valid[0] if valid else "")
    raise ConfigValidationError(
        f"Unknown model {shorthand!r}; did you mean {suggestion!r}? Valid shorthands: {valid}",
        invalid_value=shorthand,
        suggestion=suggestion,
        valid_options=valid,
    )


def validate_config(overrides: dict) -> None:
    """Validate and resolve model/effort values in overrides in place.

    Empty strings bypass validation. Raises ConfigValidationError on any invalid entry.
    """
    if not overrides:
        return

    models = _fetch_models()
    valid_efforts = sorted(_VALID_EFFORTS)

    for stage, values in overrides.items():
        model = values.get("model", "")
        effort = values.get("effort", "")

        if model:
            resolved = _resolve_shorthand(model, models)
            overrides[stage]["model"] = resolved

        if effort and effort not in _VALID_EFFORTS:
            close = get_close_matches(effort, valid_efforts, n=1, cutoff=0.0)
            suggestion = close[0] if close else valid_efforts[0]
            raise ConfigValidationError(
                f"Invalid effort {effort!r} for stage {stage!r}; "
                f"did you mean {suggestion!r}? Valid efforts: {valid_efforts}",
                invalid_value=effort,
                suggestion=suggestion,
                valid_options=valid_efforts,
            )
