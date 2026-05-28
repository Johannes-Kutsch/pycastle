from __future__ import annotations

import dataclasses
import importlib.util
import os
import re
import types
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import platformdirs

from pycastle.config.types import StageOverride
from pycastle.errors import ConfigValidationError

__all__ = [
    "Config",
    "describe_config_layers",
    "load_config",
    "resolve_dockerfile",
    "resolve_global_dir",
]

_BUG_REPORT_REPO_RE = re.compile(r"^[^/]+/[^/]+$")
_DEFAULTS_DIR = Path(__file__).resolve().parents[1] / "defaults"

_IGNORED_CONFIG_KEYS = frozenset(
    {"usage_limit_patterns", "default_service", "dockerfile"}
)

_GLOBAL_FORBIDDEN_FIELDS = frozenset(
    {
        "pycastle_dir",
        "prompts_dir",
        "logs_dir",
        "worktrees_dir",
        "env_file",
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
    enhancement_label: str = "enhancement"
    needs_triage_label: str = "needs-triage"
    needs_info_label: str = "needs-info"
    wontfix_label: str = "wontfix"
    refactor_slice_label: str = "refactor-slice"
    behavior_slice_label: str = "behavior-slice"
    docs_slice_label: str = "docs-slice"
    needs_slice_type_label: str = "needs-slice-type"
    pycastle_dir: Path = dataclasses.field(default_factory=lambda: Path("pycastle"))
    prompts_dir: Path = dataclasses.field(
        default_factory=lambda: Path("pycastle/prompts")
    )
    logs_dir: Path = dataclasses.field(default_factory=lambda: Path("pycastle/logs"))
    worktrees_dir: Path = dataclasses.field(default_factory=lambda: Path("worktrees"))
    env_file: Path = dataclasses.field(default_factory=lambda: Path("pycastle/.env"))
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
    auto_file_bugs: bool = False
    bug_report_repo: str = "Johannes-Kutsch/pycastle"
    timeout_retries: int = 1
    plan_override: StageOverride = dataclasses.field(
        default_factory=lambda: StageOverride(service="claude", effort="low")
    )
    implement_override: StageOverride = dataclasses.field(
        default_factory=lambda: StageOverride(service="claude", effort="medium")
    )
    review_override: StageOverride = dataclasses.field(
        default_factory=lambda: StageOverride(service="claude", effort="medium")
    )
    merge_override: StageOverride = dataclasses.field(
        default_factory=lambda: StageOverride(service="claude", effort="high")
    )
    preflight_issue_override: StageOverride = dataclasses.field(
        default_factory=lambda: StageOverride(service="claude", effort="high")
    )
    improve_override: StageOverride = dataclasses.field(
        default_factory=lambda: StageOverride(
            service="claude", model="opus", effort="high"
        )
    )
    improve_max: int | None = None
    improve_mode: Literal["until_sleep", "endless"] | None = None
    diagnose_on_failure: bool = True


def referenced_services(cfg: Config) -> set[str]:
    """Return the set of service names the resolved config references."""
    names: set[str] = set()
    for override in (
        cfg.plan_override,
        cfg.implement_override,
        cfg.review_override,
        cfg.merge_override,
        cfg.preflight_issue_override,
        cfg.improve_override,
    ):
        node: StageOverride | None = override
        while node is not None:
            if node.service:
                names.add(node.service)
            node = node.fallback
    return names


def resolve_dockerfile(service: str, pycastle_dir: Path) -> Path:
    local = pycastle_dir / f"Dockerfile.{service}"
    if local.exists():
        return local
    bundled = _DEFAULTS_DIR / f"Dockerfile.{service}"
    if bundled.exists():
        return bundled
    raise ConfigValidationError(
        f"Unknown service {service!r}; no bundled Dockerfile default exists",
        invalid_value=service,
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
    _validate_bug_report_repo(cfg)
    _validate_improve_max(cfg)
    _validate_improve_mode(cfg)
    return cfg


def _validate_improve_mode(cfg: Config) -> None:
    valid = {"until_sleep", "endless"}
    if cfg.improve_mode is not None and cfg.improve_mode not in valid:
        raise ConfigValidationError(
            f"Invalid improve_mode {cfg.improve_mode!r}; valid values: {sorted(valid)}",
            invalid_value=cfg.improve_mode,
            suggestion="until_sleep",
            valid_options=sorted(valid),
        )


def _validate_improve_max(cfg: Config) -> None:
    if cfg.improve_max is not None and cfg.improve_max < 1:
        raise ConfigValidationError(
            "improve_max must be >= 1",
            invalid_value=str(cfg.improve_max),
        )


def _validate_bug_report_repo(cfg: Config) -> None:
    if not _BUG_REPORT_REPO_RE.match(cfg.bug_report_repo):
        raise ConfigValidationError(
            f"Invalid bug_report_repo {cfg.bug_report_repo!r}; "
            "expected 'owner/repo' format (e.g. 'Johannes-Kutsch/pycastle')",
            invalid_value=cfg.bug_report_repo,
            suggestion="Johannes-Kutsch/pycastle",
        )


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
