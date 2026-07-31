from __future__ import annotations

import contextlib
import dataclasses
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

import platformdirs

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "PycastleLayout",
    "describe_config_layers",
    "resolve_global_dir",
    "resolve_layout",
]


def _sanitize_project_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


@dataclasses.dataclass(frozen=True)
class PycastleLayout:
    repo_root: Path
    pycastle_dir: Path
    pycastle_home: Path
    global_config_file: Path
    local_config_file: Path
    global_env_file: Path
    local_env_file: Path
    global_run_lock_path: Path
    run_markers_dir: Path
    project_run_marker_path: Path
    _display_os_name: str | None = dataclasses.field(
        default=None, repr=False, compare=False
    )
    _display_appdata: str | None = dataclasses.field(
        default=None, repr=False, compare=False
    )

    @property
    def global_config_display_path(self) -> str:
        return _display_pycastle_home_path(
            self.global_config_file,
            appdata=self._display_appdata,
            os_name=self._display_os_name,
        )

    @property
    def local_config_display_path(self) -> str:
        return "pycastle/config.py"


def resolve_global_dir(explicit: Path | None, env: Mapping[str, str]) -> Path:
    if explicit is not None:
        return explicit
    env_val = env.get("PYCASTLE_HOME")
    if env_val:
        return Path(env_val)
    return Path(platformdirs.user_config_dir("pycastle"))


def resolve_layout(
    repo_root: Path | None = None,
    pycastle_home: Path | None = None,
    env: Mapping[str, str] | None = None,
    os_name: str | None = None,
) -> PycastleLayout:
    resolved_env = os.environ if env is None else env
    resolved_repo_root = repo_root if repo_root is not None else Path.cwd()
    resolved_pycastle_dir = resolved_repo_root / "pycastle"
    resolved_pycastle_home = resolve_global_dir(pycastle_home, resolved_env)
    global_config_file = resolved_pycastle_home / "config.py"
    local_config_file = resolved_pycastle_dir / "config.py"
    run_markers_dir = resolved_pycastle_home / ".runs"
    project_run_marker_path = (
        run_markers_dir / f"{_sanitize_project_name(resolved_repo_root.name)}.lock"
    )
    return PycastleLayout(
        repo_root=resolved_repo_root,
        pycastle_dir=resolved_pycastle_dir,
        pycastle_home=resolved_pycastle_home,
        global_config_file=global_config_file,
        local_config_file=local_config_file,
        global_env_file=resolved_pycastle_home / ".env",
        local_env_file=resolved_pycastle_dir / ".env",
        global_run_lock_path=resolved_pycastle_home / ".run.lock",
        run_markers_dir=run_markers_dir,
        project_run_marker_path=project_run_marker_path,
        _display_os_name=os_name,
        _display_appdata=resolved_env.get("APPDATA"),
    )


def _display_pycastle_home_path(
    path: Path,
    *,
    appdata: str | None,
    os_name: str | None = None,
) -> str:
    if (os.name if os_name is None else os_name) == "nt" and appdata:
        with contextlib.suppress(ValueError):
            rel = path.relative_to(appdata)
            return "%APPDATA%\\" + str(rel).replace("/", "\\")
    home = Path.home()
    try:
        rel = path.relative_to(home)
        return "~/" + rel.as_posix()
    except ValueError:
        return path.as_posix()


def describe_config_layers(
    repo_root: Path | None = None,
    global_dir: Path | None = None,
    *,
    os_name: str | None = None,
) -> str:
    parts = ["defaults"]
    layout = resolve_layout(
        repo_root=repo_root,
        pycastle_home=global_dir,
        os_name=os_name,
    )
    if layout.global_config_file.exists():
        parts.append(layout.global_config_display_path)
    if layout.local_config_file.exists():
        parts.append(layout.local_config_display_path)
    return "Config: " + " + ".join(parts)
