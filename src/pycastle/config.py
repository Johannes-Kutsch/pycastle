from __future__ import annotations

import dataclasses
import importlib.util
import re
import types
from difflib import get_close_matches
from functools import lru_cache
from pathlib import Path
from typing import Any

from pycastle._types import StageOverride

_VALID_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_MODEL_RE = re.compile(r"^claude-(haiku|sonnet|opus)-(.+)$")

__all__ = ["StageOverride"]


@dataclasses.dataclass(frozen=True)
class Config:
    max_iterations: int = 10
    max_parallel: int = 1
    worktree_timeout: int = 30
    idle_timeout: int = 300
    docker_image_name: str = ""
    issue_label: str = "ready-for-agent"
    hitl_label: str = "ready-for-human"
    pycastle_dir: Path = dataclasses.field(default_factory=lambda: Path("pycastle"))
    prompts_dir: Path = dataclasses.field(
        default_factory=lambda: Path("pycastle/prompts")
    )
    logs_dir: Path = dataclasses.field(default_factory=lambda: Path("pycastle/logs"))
    worktrees_dir: Path = dataclasses.field(default_factory=lambda: Path("worktrees"))
    env_file: Path = dataclasses.field(default_factory=lambda: Path("pycastle/.env"))
    dockerfile: Path = dataclasses.field(
        default_factory=lambda: Path("pycastle/Dockerfile")
    )
    preflight_checks: tuple[tuple[str, str], ...] = dataclasses.field(
        default_factory=lambda: (
            ("ruff", "ruff check ."),
            ("mypy", "mypy ."),
            ("pytest", "pytest"),
        )
    )
    implement_checks: tuple[str, ...] = dataclasses.field(
        default_factory=lambda: (
            "ruff check --fix",
            "ruff format --check",
            "mypy .",
            "pytest",
        )
    )
    usage_limit_patterns: tuple[str, ...] = dataclasses.field(
        default_factory=lambda: ("You've hit your", "Credit balance is too low")
    )
    plan_override: StageOverride = dataclasses.field(default_factory=StageOverride)
    implement_override: StageOverride = dataclasses.field(default_factory=StageOverride)
    review_override: StageOverride = dataclasses.field(default_factory=StageOverride)
    merge_override: StageOverride = dataclasses.field(default_factory=StageOverride)


def _parse_version(version_str: str) -> tuple[int, ...]:
    parts = re.split(r"[-.]", version_str)
    return tuple(int(p) if p.isdigit() else 0 for p in parts)


@lru_cache(maxsize=None)
def _fetch_models(claude_service: Any) -> tuple[str, ...]:
    from .errors import ClaudeServiceError, ConfigValidationError

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
    from .errors import ConfigValidationError

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


def _validate_and_resolve(cfg: Config, claude_service: Any) -> Config:
    from .errors import ConfigValidationError

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


def load_config(
    repo_root: Path | None = None,
    overrides: dict[str, Any] | None = None,
    *,
    validate: bool = False,
    claude_service: Any | None = None,
) -> Config:
    """Load defaults, apply project-local pycastle/config.py, then apply any extra overrides."""
    kwargs: dict[str, Any] = {}
    valid_fields = {f.name for f in dataclasses.fields(Config)}

    root = repo_root if repo_root is not None else Path.cwd()
    local = root / "pycastle" / "config.py"
    if local.exists():
        spec = importlib.util.spec_from_file_location("_pycastle_local_config", local)
        if spec is not None and spec.loader is not None:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for k, v in vars(mod).items():
                if k.startswith("_"):
                    continue
                if isinstance(v, (type, types.ModuleType)):
                    continue
                if k not in valid_fields:
                    raise ValueError(f"Unknown config key: {k!r}")
                kwargs[k] = v

    if overrides is not None:
        for k, v in overrides.items():
            if k not in valid_fields:
                raise ValueError(f"Unknown config key: {k!r}")
            kwargs[k] = v

    cfg = Config(**kwargs)
    if validate:
        from .claude_service import ClaudeService

        cs = claude_service if claude_service is not None else ClaudeService()
        cfg = _validate_and_resolve(cfg, cs)
    return cfg


config: Config = load_config()

# Backward-compatibility aliases for consumer modules still using UPPERCASE names.
MAX_ITERATIONS = config.max_iterations
MAX_PARALLEL = config.max_parallel
WORKTREE_TIMEOUT = config.worktree_timeout
IDLE_TIMEOUT = config.idle_timeout
DOCKER_IMAGE_NAME = config.docker_image_name
ISSUE_LABEL = config.issue_label
HITL_LABEL = config.hitl_label
PYCASTLE_DIR = config.pycastle_dir
PROMPTS_DIR = config.prompts_dir
LOGS_DIR = config.logs_dir
WORKTREES_DIR = config.worktrees_dir
ENV_FILE = config.env_file
DOCKERFILE = config.dockerfile
PREFLIGHT_CHECKS = list(config.preflight_checks)
IMPLEMENT_CHECKS = list(config.implement_checks)
USAGE_LIMIT_PATTERNS = list(config.usage_limit_patterns)
STAGE_OVERRIDES: dict[str, dict[str, str]] = {
    "plan": {
        "model": config.plan_override.model,
        "effort": config.plan_override.effort,
    },
    "implement": {
        "model": config.implement_override.model,
        "effort": config.implement_override.effort,
    },
    "review": {
        "model": config.review_override.model,
        "effort": config.review_override.effort,
    },
    "merge": {
        "model": config.merge_override.model,
        "effort": config.merge_override.effort,
    },
}
