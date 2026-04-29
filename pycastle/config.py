from pathlib import Path

from pycastle.config import StageOverride

# --- Behaviour ---
max_iterations = 10
max_parallel = 4
worktree_timeout = 30
idle_timeout = 300

# --- Docker ---
docker_image_name = "pycastle"

# --- Labels ---
issue_label = "ready-for-agent"

# --- Paths ---
pycastle_dir = Path("pycastle")
prompts_dir = Path("pycastle/prompts")
logs_dir = Path("pycastle/logs")
worktrees_dir = Path("worktrees")
env_file = Path("pycastle/.env")
dockerfile = Path("pycastle/Dockerfile")

# --- Stage overrides ---
# model shorthands: haiku, sonnet, opus  (leave empty to use the Claude CLI default)
# effort values:    low, medium, high, xhigh, max    (leave empty to use the Claude CLI default)
plan_override = StageOverride(model="haiku", effort="low")
implement_override = StageOverride(model="sonnet", effort="medium")
review_override = StageOverride(model="sonnet", effort="high")
merge_override = StageOverride(model="sonnet", effort="medium")
