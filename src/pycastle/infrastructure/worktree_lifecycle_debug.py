from __future__ import annotations

import json
import os
import stat
from datetime import datetime, UTC
from pathlib import Path

from ..config import Config, resolve_logs_dir


_ENV_LOG_PATH = "PYCASTLE_WORKTREE_LIFECYCLE_DEBUG_LOG"


def _node_type(stat_result: os.stat_result) -> str:
    file_mode = stat_result.st_mode
    if stat.S_ISLNK(file_mode):
        return "symlink"
    if stat.S_ISDIR(file_mode):
        return "directory"
    if stat.S_ISREG(file_mode):
        return "file"
    return "other"


def _path_snapshot(path: Path) -> tuple[bool, str, int | None, int | None]:
    try:
        stat_result = path.lstat()
    except FileNotFoundError:
        return False, "missing", None, None
    return True, _node_type(stat_result), stat_result.st_uid, stat_result.st_gid


def _log_target(
    *, cfg: Config | None = None, repo_root: Path | None = None
) -> Path | None:
    env_path = os.getenv(_ENV_LOG_PATH)
    if env_path:
        try:
            return Path(env_path)
        except Exception:
            return None
    if cfg is not None:
        try:
            return resolve_logs_dir(cfg) / "worktree-lifecycle-debug.log"
        except Exception:
            return None
    if repo_root is not None:
        try:
            from ..config import load_config

            loaded_cfg = load_config(repo_root=repo_root)
            return resolve_logs_dir(loaded_cfg) / "worktree-lifecycle-debug.log"
        except Exception:
            return None
    return None


def log_worktree_lifecycle_event(
    event: str,
    path: Path,
    *,
    cfg: Config | None = None,
    repo_root: Path | None = None,
) -> None:
    """Write one diagnostic lifecycle event in a temporary, easy-to-remove format."""
    try:
        log_path = _log_target(cfg=cfg, repo_root=repo_root)
        if log_path is None:
            return

        exists, node_type, uid, gid = _path_snapshot(path)
        entry = {
            "event": event,
            "temporary_diagnostic": "issue-1848",
            "path": str(path),
            "exists": exists,
            "node_type": node_type,
            "uid": uid,
            "gid": gid,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        return
