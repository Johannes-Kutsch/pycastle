import importlib.util as _util
import sys as _sys
from pathlib import Path

from .defaults.config import (  # noqa: F401
    DOCKERFILE,
    DOCKER_IMAGE_NAME,
    ENV_FILE,
    HITL_LABEL,
    IDLE_TIMEOUT,
    IMPLEMENT_CHECKS,
    ISSUE_LABEL,
    LOGS_DIR,
    MAX_ITERATIONS,
    MAX_PARALLEL,
    PREFLIGHT_CHECKS,
    PROMPTS_DIR,
    PYCASTLE_DIR,
    STAGE_OVERRIDES,
    USAGE_LIMIT_PATTERNS,
    WORKTREE_TIMEOUT,
    WORKTREES_DIR,
)

_local = Path(__file__).parent.parent.parent / "pycastle" / "config.py"
if _local.exists():
    _spec = _util.spec_from_file_location("_pycastle_local_config", _local)
    if _spec is not None and _spec.loader is not None:
        _mod = _util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _me = _sys.modules[__name__]
        for _k, _v in vars(_mod).items():
            if not _k.startswith("_"):
                setattr(_me, _k, _v)
