from __future__ import annotations

import dataclasses
import importlib.util
import types
from difflib import get_close_matches
from pathlib import Path
from typing import Any

from pycastle._types import StageOverride
from pycastle.errors import ConfigValidationError

__all__ = ["Config", "load_config"]

_VALID_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})

_IGNORED_CONFIG_KEYS = frozenset({"usage_limit_patterns"})


@dataclasses.dataclass(frozen=True)
class Config:
    max_iterations: int = 10
    max_parallel: int = 1
    worktree_timeout: int = 30
    idle_timeout: int = 300
    docker_image_name: str = ""
    bug_label: str = "bug"
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
    auto_push: bool = True
    timeout_retries: int = 1
    plan_override: StageOverride = dataclasses.field(default_factory=StageOverride)
    implement_override: StageOverride = dataclasses.field(default_factory=StageOverride)
    review_override: StageOverride = dataclasses.field(default_factory=StageOverride)
    merge_override: StageOverride = dataclasses.field(default_factory=StageOverride)


def load_config(
    repo_root: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> Config:
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
                if k in _IGNORED_CONFIG_KEYS:
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
    return _validate_efforts(cfg)


def _validate_efforts(cfg: Config) -> Config:
    valid_efforts = sorted(_VALID_EFFORTS)
    stage_overrides = {
        "plan": cfg.plan_override,
        "implement": cfg.implement_override,
        "review": cfg.review_override,
        "merge": cfg.merge_override,
    }
    for stage, override in stage_overrides.items():
        effort = override.effort
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
    return cfg
