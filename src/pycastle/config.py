import re
from pathlib import Path

# Override with local pycastle/config.py if present in CWD
import importlib.util as _util
import sys as _sys

_local = Path("pycastle/config.py")
if _local.exists():
    _spec = _util.spec_from_file_location("_pycastle_local_config", _local)
    if _spec is not None and _spec.loader is not None:
        _mod = _util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _me = _sys.modules[__name__]
        for _k, _v in vars(_mod).items():
            if not _k.startswith("_"):
                setattr(_me, _k, _v)


MAX_ITERATIONS = 10
MAX_PARALLEL = 1
DOCKER_IMAGE = "pycastle"
ISSUE_LABEL = "ready-for-agent"
PYCASTLE_DIR = Path("pycastle")
PROMPTS_DIR = Path("pycastle/prompts")
LOGS_DIR = Path("pycastle/logs")
WORKTREES_DIR = Path("worktrees")
ENV_FILE = Path("pycastle/.env")
DOCKERFILE = Path("pycastle/Dockerfile")

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
SHELL_EXPR = re.compile(r"!`([^`]+)`")
