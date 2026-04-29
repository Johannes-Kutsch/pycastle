import dataclasses
import importlib.util
import types
from pathlib import Path
from typing import Any


@dataclasses.dataclass(frozen=True)
class StageOverride:
    model: str = ""
    effort: str = ""


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


def load_config(
    repo_root: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> Config:
    """Load defaults, apply project-local pycastle/config.py, then apply any extra overrides."""
    kwargs: dict[str, Any] = {}
    valid_fields = {f.name for f in dataclasses.fields(Config)}

    root = repo_root if repo_root is not None else Path(__file__).parent.parent.parent
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

    if overrides:
        for k, v in overrides.items():
            if k not in valid_fields:
                raise ValueError(f"Unknown config key: {k!r}")
            kwargs[k] = v

    return Config(**kwargs)


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
