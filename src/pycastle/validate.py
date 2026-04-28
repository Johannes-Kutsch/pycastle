from __future__ import annotations

import re
from difflib import get_close_matches
from functools import lru_cache

from .claude_service import ClaudeService
from .errors import ClaudeServiceError, ConfigValidationError

_VALID_EFFORTS = frozenset({"low", "normal", "high"})
_MODEL_RE = re.compile(r"^claude-(haiku|sonnet|opus)-(.+)$")
_DEFAULT_CLAUDE_SERVICE = ClaudeService()


def _parse_version(version_str: str) -> tuple[int, ...]:
    parts = re.split(r"[-.]", version_str)
    return tuple(int(p) if p.isdigit() else 0 for p in parts)


@lru_cache(maxsize=None)
def _fetch_models(claude_service: ClaudeService) -> tuple[str, ...]:
    try:
        return claude_service.list_models()
    except ClaudeServiceError as exc:
        raise ConfigValidationError(str(exc)) from exc


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


def validate_config(
    overrides: dict, *, claude_service: ClaudeService | None = None
) -> None:
    """Validate and resolve model/effort values in overrides in place.

    Empty strings bypass validation. Raises ConfigValidationError on any invalid entry.
    All entries are validated before any mutations are applied.
    """
    if not overrides:
        return

    cs = claude_service if claude_service is not None else _DEFAULT_CLAUDE_SERVICE
    valid_efforts = sorted(_VALID_EFFORTS)
    resolved_models: dict[str, str] = {}

    for stage, values in overrides.items():
        model = values.get("model", "")
        effort = values.get("effort", "")

        if model:
            resolved_models[stage] = _resolve_shorthand(model, _fetch_models(cs))

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

    for stage, resolved_model in resolved_models.items():
        overrides[stage]["model"] = resolved_model
