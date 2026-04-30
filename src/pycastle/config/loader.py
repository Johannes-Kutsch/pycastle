from __future__ import annotations

import dataclasses
import importlib.util
import types
from pathlib import Path
from typing import Any

from pycastle._types import StageOverride

__all__ = ["Config", "load_config"]


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
    timeout_retries: int = 1
    usage_limit_patterns: tuple[str, ...] = dataclasses.field(
        default_factory=lambda: ("You've hit your", "Credit balance is too low")
    )
    plan_override: StageOverride = dataclasses.field(default_factory=StageOverride)
    implement_override: StageOverride = dataclasses.field(default_factory=StageOverride)
    review_override: StageOverride = dataclasses.field(default_factory=StageOverride)
    merge_override: StageOverride = dataclasses.field(default_factory=StageOverride)


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
        from pycastle.claude_service import ClaudeService

        from .validator import validate_config

        cs = claude_service if claude_service is not None else ClaudeService()
        cfg = validate_config(cfg, cs)
    return cfg
