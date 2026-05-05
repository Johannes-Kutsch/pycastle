from __future__ import annotations

import dataclasses
import importlib.util
import os
import re
import types
from collections.abc import Mapping
from difflib import get_close_matches
from pathlib import Path
from typing import Any

import platformdirs

from pycastle._types import StageOverride
from pycastle.errors import ConfigValidationError

__all__ = ["Config", "describe_config_layers", "load_config", "resolve_global_dir"]

_VALID_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})

_IGNORED_CONFIG_KEYS = frozenset({"usage_limit_patterns"})

_GLOBAL_FORBIDDEN_FIELDS = frozenset(
    {
        "pycastle_dir",
        "prompts_dir",
        "logs_dir",
        "worktrees_dir",
        "env_file",
        "dockerfile",
        "docker_image_name",
    }
)


def derive_docker_image_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


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
    preflight_issue_override: StageOverride = dataclasses.field(
        default_factory=StageOverride
    )


def resolve_global_dir(explicit: Path | None, env: Mapping[str, str]) -> Path:
    if explicit is not None:
        return explicit
    env_val = env.get("PYCASTLE_HOME")
    if env_val:
        return Path(env_val)
    return Path(platformdirs.user_config_dir("pycastle"))


def _display_global_path(path: Path) -> str:
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            try:
                rel = path.relative_to(appdata)
                return "%APPDATA%\\" + str(rel).replace("/", "\\")
            except ValueError:
                pass
    home = Path.home()
    try:
        rel = path.relative_to(home)
        return "~/" + rel.as_posix()
    except ValueError:
        return path.as_posix()


def describe_config_layers(
    repo_root: Path | None = None,
    global_dir: Path | None = None,
) -> str:
    parts = ["defaults"]
    resolved_global = resolve_global_dir(global_dir, os.environ)
    global_file = resolved_global / "config.py"
    if global_file.exists():
        parts.append(_display_global_path(global_file))
    root = repo_root if repo_root is not None else Path.cwd()
    local = root / "pycastle" / "config.py"
    if local.exists():
        parts.append("pycastle/config.py")
    return "Config: " + " + ".join(parts)


def load_config(
    repo_root: Path | None = None,
    overrides: dict[str, Any] | None = None,
    global_dir: Path | None = None,
) -> Config:
    kwargs: dict[str, Any] = {}
    valid_fields = {f.name for f in dataclasses.fields(Config)}

    resolved_global = resolve_global_dir(global_dir, os.environ)
    global_file = resolved_global / "config.py"
    if global_file.exists():
        global_kwargs = _read_config_file(
            global_file, "_pycastle_global_config", valid_fields
        )
        forbidden = sorted(_GLOBAL_FORBIDDEN_FIELDS & global_kwargs.keys())
        if forbidden:
            raise ConfigValidationError(
                "Global-forbidden fields are not allowed in global config.py; "
                f"offending field(s): {forbidden}",
                invalid_value=", ".join(forbidden),
            )
        kwargs.update(global_kwargs)

    root = repo_root if repo_root is not None else Path.cwd()
    local = root / "pycastle" / "config.py"
    if local.exists():
        kwargs.update(_read_config_file(local, "_pycastle_local_config", valid_fields))

    if overrides is not None:
        for k, v in overrides.items():
            if k not in valid_fields:
                raise ValueError(f"Unknown config key: {k!r}")
            kwargs[k] = v

    cfg = Config(**kwargs)
    if cfg.docker_image_name == "":
        cfg = dataclasses.replace(
            cfg, docker_image_name=derive_docker_image_name(Path.cwd().name)
        )
    return _validate_efforts(cfg)


def _read_config_file(
    path: Path, module_name: str, valid_fields: set[str]
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return result
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
        result[k] = v
    return result


def _validate_efforts(cfg: Config) -> Config:
    valid_efforts = sorted(_VALID_EFFORTS)
    stage_overrides = {
        "plan": cfg.plan_override,
        "implement": cfg.implement_override,
        "review": cfg.review_override,
        "merge": cfg.merge_override,
        "preflight_issue": cfg.preflight_issue_override,
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
