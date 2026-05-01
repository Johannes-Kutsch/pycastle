from __future__ import annotations

import dataclasses
import re
from difflib import get_close_matches
from functools import lru_cache
from typing import Any

from pycastle._types import StageOverride
from pycastle.config.loader import Config

_VALID_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_MODEL_RE = re.compile(r"^claude-(haiku|sonnet|opus)-(.+)$")

__all__ = ["validate_config"]


def _parse_version(version_str: str) -> tuple[int, ...]:
    parts = re.split(r"[-.]", version_str)
    return tuple(int(p) if p.isdigit() else 0 for p in parts)


@lru_cache(maxsize=None)
def _fetch_models(claude_service: Any) -> tuple[str, ...]:
    from pycastle.errors import ClaudeCliNotFoundError, ClaudeServiceError, ConfigValidationError

    try:
        return claude_service.list_models()
    except ClaudeCliNotFoundError:
        raise
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
    from pycastle.errors import ConfigValidationError

    if shorthand in models:
        return shorthand

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


def validate_config(cfg: Config, claude_service: Any) -> Config:
    from pycastle.errors import ConfigValidationError

    overrides = {
        "plan": cfg.plan_override,
        "implement": cfg.implement_override,
        "review": cfg.review_override,
        "merge": cfg.merge_override,
    }
    valid_efforts = sorted(_VALID_EFFORTS)
    resolved_models: dict[str, str] = {}

    for stage, override in overrides.items():
        model = override.model
        effort = override.effort

        if model:
            resolved_models[stage] = _resolve_shorthand(
                model, _fetch_models(claude_service)
            )

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

    def _resolved_override(stage: str, orig: StageOverride) -> StageOverride:
        if stage in resolved_models:
            return StageOverride(model=resolved_models[stage], effort=orig.effort)
        return orig

    return dataclasses.replace(
        cfg,
        plan_override=_resolved_override("plan", cfg.plan_override),
        implement_override=_resolved_override("implement", cfg.implement_override),
        review_override=_resolved_override("review", cfg.review_override),
        merge_override=_resolved_override("merge", cfg.merge_override),
    )
