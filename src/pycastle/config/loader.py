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
import pycastle_agent_runtime as agent_runtime

from pycastle._universal_image_build import resolve_universal_dockerfile
from pycastle.config.types import StageOverride
from pycastle.errors import ConfigValidationError
from pycastle.label_catalog import CANONICAL_LABEL_DEFAULTS

__all__ = [
    "Config",
    "describe_config_layers",
    "image_name_for",
    "load_config",
    "replace_config_runtime_fields",
    "resolve_logs_dir",
    "resolve_dockerfile",
    "resolve_global_dir",
]

_BUG_REPORT_REPO_RE = re.compile(r"^[^/]+/[^/]+$")
_DEFAULTS_DIR = Path(__file__).resolve().parents[1] / "defaults"

_REMOVED_PROJECT_LOCAL_PATH_KEYS = frozenset(
    {
        "dockerfile",
        "pycastle_dir",
        "prompts_dir",
        "worktrees_dir",
        "env_file",
    }
)

_LEGACY_IGNORED_CONFIG_KEYS = frozenset(
    {
        "usage_limit_patterns",
        "default_service",
    }
)

_IGNORED_CONFIG_KEYS = _REMOVED_PROJECT_LOCAL_PATH_KEYS | _LEGACY_IGNORED_CONFIG_KEYS

_GLOBAL_FORBIDDEN_FIELDS = frozenset(
    {
        "docker_image_name",
    }
)


@dataclasses.dataclass(frozen=True)
class _ResolvedPycastlePaths:
    repo_root: Path
    pycastle_dir: Path
    global_dir: Path
    global_config_file: Path
    local_config_file: Path
    global_env_file: Path
    local_env_file: Path


def derive_docker_image_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def image_name_for(base: str, service: str) -> str:
    return base


@dataclasses.dataclass(frozen=True)
class Config:
    max_iterations: int = 10
    max_parallel: int = 1
    worktree_timeout: int = 30
    idle_timeout: int = 300
    docker_image_name: str = ""
    bug_label: str = CANONICAL_LABEL_DEFAULTS["bug_label"]
    issue_label: str = CANONICAL_LABEL_DEFAULTS["issue_label"]
    hitl_label: str = CANONICAL_LABEL_DEFAULTS["hitl_label"]
    enhancement_label: str = CANONICAL_LABEL_DEFAULTS["enhancement_label"]
    needs_triage_label: str = CANONICAL_LABEL_DEFAULTS["needs_triage_label"]
    needs_info_label: str = CANONICAL_LABEL_DEFAULTS["needs_info_label"]
    wontfix_label: str = CANONICAL_LABEL_DEFAULTS["wontfix_label"]
    refactor_slice_label: str = CANONICAL_LABEL_DEFAULTS["refactor_slice_label"]
    behavior_slice_label: str = CANONICAL_LABEL_DEFAULTS["behavior_slice_label"]
    docs_slice_label: str = CANONICAL_LABEL_DEFAULTS["docs_slice_label"]
    needs_slice_type_label: str = CANONICAL_LABEL_DEFAULTS["needs_slice_type_label"]
    logs_dir: Path = dataclasses.field(default_factory=lambda: Path("pycastle/logs"))
    preflight_checks: tuple[tuple[str, str], ...] = dataclasses.field(
        default_factory=lambda: (
            ("ruff", "ruff check ."),
            ("mypy", "mypy ."),
            ("pytest", "pytest"),
        )
    )
    host_checks: tuple[tuple[str, str], ...] = dataclasses.field(
        default_factory=lambda: (("pytest", "pytest"),)
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
        default_factory=lambda: StageOverride(
            service="opencode",
            model="deepseek-v4-flash",
            effort="medium",
            fallback=StageOverride(
                service="codex",
                model="gpt-5.4-mini",
                effort="low",
                fallback=StageOverride(service="claude", model="haiku", effort="low"),
            ),
        )
    )
    implement_override: StageOverride = dataclasses.field(
        default_factory=lambda: StageOverride(
            service="codex",
            model="gpt-5.4",
            effort="medium",
            fallback=StageOverride(service="claude", model="sonnet", effort="medium"),
        )
    )
    review_override: StageOverride = dataclasses.field(
        default_factory=lambda: StageOverride(
            service="claude",
            model="sonnet",
            effort="medium",
            fallback=StageOverride(service="codex", model="gpt-5.4", effort="medium"),
        )
    )
    merge_override: StageOverride = dataclasses.field(
        default_factory=lambda: StageOverride(
            service="codex",
            model="gpt-5.5",
            effort="medium",
            fallback=StageOverride(service="claude", model="opus", effort="high"),
        )
    )
    preflight_issue_override: StageOverride = dataclasses.field(
        default_factory=lambda: StageOverride(
            service="codex",
            model="gpt-5.5",
            effort="medium",
            fallback=StageOverride(service="claude", model="opus", effort="high"),
        )
    )
    improve_override: StageOverride = dataclasses.field(
        default_factory=lambda: StageOverride(
            service="codex",
            model="gpt-5.5",
            effort="high",
            fallback=StageOverride(service="claude", model="opus", effort="high"),
        )
    )
    improve_max: int | None = None
    improve_mode: Literal["until_sleep", "endless"] | None = None
    diagnose_on_failure: bool = True
    repo_root: Path = dataclasses.field(
        default_factory=Path.cwd, init=False, repr=False, compare=False
    )
    _global_logs_dir_parent: bool = dataclasses.field(
        default=False, init=False, repr=False, compare=False
    )


def referenced_services(cfg: Config) -> set[str]:
    """Return the set of service names the resolved config references."""
    return {
        service
        for override in (
            cfg.plan_override,
            cfg.implement_override,
            cfg.review_override,
            cfg.merge_override,
            cfg.preflight_issue_override,
            cfg.improve_override,
        )
        for service in agent_runtime.referenced_service_names(override)
    }


def resolve_dockerfile(pycastle_dir: Path | str) -> Path:
    return resolve_universal_dockerfile(
        pycastle_dir,
        bundled_defaults_dir=_DEFAULTS_DIR,
    )


def resolve_global_dir(explicit: Path | None, env: Mapping[str, str]) -> Path:
    if explicit is not None:
        return explicit
    env_val = env.get("PYCASTLE_HOME")
    if env_val:
        return Path(env_val)
    return Path(platformdirs.user_config_dir("pycastle"))


def _resolve_pycastle_paths(
    repo_root: Path | None,
    global_dir: Path | None,
    env: Mapping[str, str],
) -> _ResolvedPycastlePaths:
    resolved_repo_root = repo_root if repo_root is not None else Path.cwd()
    resolved_pycastle_dir = resolved_repo_root / "pycastle"
    resolved_global_dir = resolve_global_dir(global_dir, env)
    return _ResolvedPycastlePaths(
        repo_root=resolved_repo_root,
        pycastle_dir=resolved_pycastle_dir,
        global_dir=resolved_global_dir,
        global_config_file=resolved_global_dir / "config.py",
        local_config_file=resolved_pycastle_dir / "config.py",
        global_env_file=resolved_global_dir / ".env",
        local_env_file=resolved_pycastle_dir / ".env",
    )


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
    paths = _resolve_pycastle_paths(repo_root, global_dir, os.environ)
    if paths.global_config_file.exists():
        parts.append(_display_global_path(paths.global_config_file))
    if paths.local_config_file.exists():
        parts.append("pycastle/config.py")
    return "Config: " + " + ".join(parts)


def resolve_logs_dir(cfg: Config) -> Path:
    logs_dir = cfg.logs_dir
    if not logs_dir.is_absolute():
        logs_dir = (cfg.repo_root / logs_dir).resolve()
    if cfg._global_logs_dir_parent:
        return logs_dir / derive_docker_image_name(cfg.repo_root.name)
    return logs_dir


def replace_config_runtime_fields(cfg: Config, updated: Config) -> Config:
    object.__setattr__(updated, "repo_root", cfg.repo_root)
    object.__setattr__(
        updated,
        "_global_logs_dir_parent",
        cfg._global_logs_dir_parent and updated.logs_dir == cfg.logs_dir,
    )
    return updated


def load_config(
    repo_root: Path | None = None,
    overrides: dict[str, Any] | None = None,
    global_dir: Path | None = None,
) -> Config:
    kwargs: dict[str, Any] = {}
    valid_fields = {f.name for f in dataclasses.fields(Config) if f.init}
    global_logs_dir_set = False
    paths = _resolve_pycastle_paths(repo_root, global_dir, os.environ)

    if paths.global_config_file.exists():
        global_kwargs = _read_config_file(
            paths.global_config_file, "_pycastle_global_config", valid_fields
        )
        forbidden = sorted(_GLOBAL_FORBIDDEN_FIELDS & global_kwargs.keys())
        if forbidden:
            raise ConfigValidationError(
                "Global-forbidden fields are not allowed in global config.py; "
                f"offending field(s): {forbidden}",
                invalid_value=", ".join(forbidden),
            )
        global_logs_dir_set = "logs_dir" in global_kwargs
        kwargs.update(global_kwargs)

    local_logs_dir_set = False
    if paths.local_config_file.exists():
        local_kwargs = _read_config_file(
            paths.local_config_file, "_pycastle_local_config", valid_fields
        )
        local_logs_dir_set = "logs_dir" in local_kwargs
        kwargs.update(local_kwargs)

    if overrides is not None:
        for k, v in overrides.items():
            if k not in valid_fields:
                raise ValueError(f"Unknown config key: {k!r}")
            kwargs[k] = v

    cfg = Config(**kwargs)
    if cfg.docker_image_name == "":
        cfg = dataclasses.replace(
            cfg, docker_image_name=derive_docker_image_name(paths.repo_root.name)
        )
    _validate_bug_report_repo(cfg)
    _validate_improve_max(cfg)
    _validate_improve_mode(cfg)
    object.__setattr__(cfg, "repo_root", paths.repo_root)
    object.__setattr__(
        cfg,
        "_global_logs_dir_parent",
        global_logs_dir_set
        and not local_logs_dir_set
        and "logs_dir" not in (overrides or {}),
    )
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
