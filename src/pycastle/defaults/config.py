from pathlib import Path

# --- Behaviour ---
MAX_ITERATIONS = 10
MAX_PARALLEL = 1
WORKTREE_TIMEOUT = 30
IDLE_TIMEOUT = 300

# --- Docker ---
DOCKER_IMAGE_NAME = ""

# --- Labels ---
ISSUE_LABEL = "ready-for-agent"
HITL_LABEL = "ready-for-human"

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
STAGE_OVERRIDES: dict[str, dict[str, str]] = {
    "plan": {"model": "", "effort": ""},
    "implement": {"model": "", "effort": ""},
    "review": {"model": "", "effort": ""},
    "merge": {"model": "", "effort": ""},
}
