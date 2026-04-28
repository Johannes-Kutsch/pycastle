from pathlib import Path

# --- Behaviour ---
MAX_ITERATIONS = 10
MAX_PARALLEL = 4
WORKTREE_TIMEOUT = 30
IDLE_TIMEOUT = 300

# --- Docker ---
DOCKER_IMAGE_NAME = "pycastle"

# --- Labels ---
ISSUE_LABEL = "ready-for-agent"

# --- Paths ---
PYCASTLE_DIR = Path("pycastle")
PROMPTS_DIR = Path("pycastle/prompts")
LOGS_DIR = Path("pycastle/logs")
WORKTREES_DIR = Path("worktrees")
ENV_FILE = Path("pycastle/.env")
DOCKERFILE = Path("pycastle/Dockerfile")

# --- Checks ---
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

# --- Stage overrides ---
# model shorthands: haiku, sonnet, opus  (leave empty to use the Claude CLI default)
# effort values:    low, medium, high, xhigh, max    (leave empty to use the Claude CLI default)
STAGE_OVERRIDES: dict[str, dict[str, str]] = {
    "plan": {"model": "haiku", "effort": "low"},
    "implement": {"model": "sonnet", "effort": "medium"},
    "review": {"model": "sonnet", "effort": "high"},
    "merge": {"model": "sonnet", "effort": "medium"},
}
