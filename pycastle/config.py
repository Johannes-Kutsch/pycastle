import re
from pathlib import Path

MAX_ITERATIONS = 10
MAX_PARALLEL = 4
WORKTREE_TIMEOUT = 30
IDLE_TIMEOUT = 300
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

# Per-stage model and effort overrides.
# model shorthands: haiku, sonnet, opus  (leave empty to use the Claude CLI default)
# effort values:    low, normal, high    (leave empty to use the Claude CLI default)
STAGE_OVERRIDES: dict[str, dict[str, str]] = {
    "plan": {"model": "haiku", "effort": "low"},
    "implement": {"model": "sonnet", "effort": "normal"},
    "review": {"model": "sonnet", "effort": "normal"},
    "merge": {"model": "haiku", "effort": "low"},
}
