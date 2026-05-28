from pycastle import StageOverride

# --- Behaviour ---
# max_iterations = 10
# max_parallel = 1
# worktree_timeout = 30
# idle_timeout = 300
# auto_push = True

# --- Docker ---
# Name for the Docker image built by `pycastle build`.
# Defaults to a sanitised CWD name; uncomment to override.
# docker_image_name = ""

# --- Labels ---
# issue_label = "ready-for-agent"
# hitl_label = "ready-for-human"

# --- Paths ---
# pycastle_dir = Path("pycastle")
# prompts_dir = Path("pycastle/prompts")
# logs_dir = Path("pycastle/logs")
# worktrees_dir = Path("worktrees")
# env_file = Path("pycastle/.env")
# dockerfile = Path("pycastle/Dockerfile")

# --- Preflight checks (run before each agent; format: (name, command)) ---
# preflight_checks = (
#     ("ruff", "ruff check ."),
#     ("mypy", "mypy ."),
#     ("pytest", "pytest"),
# )

# --- Implement checks (run after each implement phase) ---
# implement_checks = (
#     "ruff check --fix",
#     "ruff format --check",
#     "mypy .",
#     "pytest",
# )

# --- Improve ---
# Default improve mode used when --improve is not passed on the CLI.
# "until_sleep" exits after the first sleep clears the backlog; "endless" runs until Ctrl-C.
# improve_mode = "until_sleep"

# Maximum number of improve-agent dispatches per run. improve_mode must also
# be active (--improve flag or improve_mode config) for this to have any effect.
# improve_max = 1

# --- Stage overrides ---
# model shorthands: haiku, sonnet, opus  (leave empty to use the Claude CLI default)
# effort values:    low, medium, high, xhigh, max    (leave empty to use the Claude CLI default)
plan_override = StageOverride(
    service="codex",
    model="gpt-5.4-mini",
    effort="low",
    fallback=StageOverride(service="claude", model="haiku", effort="low"),
)
implement_override = StageOverride(
    service="codex",
    model="gpt-5.4",
    effort="medium",
    fallback=StageOverride(service="claude", model="sonnet", effort="medium"),
)
review_override = StageOverride(
    service="claude",
    model="sonnet",
    effort="medium",
    fallback=StageOverride(service="codex", model="gpt-5.4", effort="medium"),
)
merge_override = StageOverride(
    service="codex",
    model="gpt-5.5",
    effort="medium",
    fallback=StageOverride(service="claude", model="opus", effort="high"),
)
preflight_issue_override = StageOverride(
    service="codex",
    model="gpt-5.5",
    effort="medium",
    fallback=StageOverride(service="claude", model="opus", effort="high"),
)
improve_override = StageOverride(
    service="codex",
    model="gpt-5.5",
    effort="high",
    fallback=StageOverride(service="claude", model="opus", effort="high"),
)
