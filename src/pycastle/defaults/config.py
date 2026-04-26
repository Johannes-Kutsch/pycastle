import re
from pathlib import Path

MAX_ITERATIONS = 10
MAX_PARALLEL = 1
WORKTREE_TIMEOUT = 30
IDLE_TIMEOUT = 300
DOCKER_IMAGE = ""
ISSUE_LABEL = "ready-for-agent"
PYCASTLE_DIR = Path("pycastle")
PROMPTS_DIR = Path("pycastle/prompts")
LOGS_DIR = Path("pycastle/logs")
WORKTREES_DIR = Path("worktrees")
ENV_FILE = Path("pycastle/.env")
DOCKERFILE = Path("pycastle/Dockerfile")

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
SHELL_EXPR = re.compile(r"!`([^`]+)`")

PREFLIGHT_CHECKS: list[tuple[str, str]] = [
    ("ruff", "ruff check ."),
    ("mypy", "mypy ."),
    ("pytest", "pytest"),
]

IMPLEMENT_CHECKS: list[str] = [
    "ruff check --fix",
    "ruff format --check",
    "mypy .",
    "pytest",
]
