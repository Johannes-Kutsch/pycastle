# pycastle project-local config — override any Config field by assigning it here.
# All values are optional; omit a field to keep the built-in default.
#
# docker_image_name: name for the Docker image built by `pycastle build`.
docker_image_name = ""

# max_iterations: max agent loop iterations per issue (default: 10).
# max_iterations = 10

# max_parallel: max issues processed concurrently (default: 1).
# max_parallel = 1

# worktree_timeout: minutes before an idle worktree is killed (default: 30).
# worktree_timeout = 30

# idle_timeout: seconds of agent inactivity before a task is abandoned (default: 300).
# idle_timeout = 300

# issue_label: GitHub label that marks issues ready for the agent (default: "ready-for-agent").
# issue_label = "ready-for-agent"

# hitl_label: GitHub label that marks issues needing human review (default: "ready-for-human").
# hitl_label = "ready-for-human"

# preflight_checks: list of (name, shell command) pairs run before each agent run.
# preflight_checks = (
#     ("ruff", "ruff check ."),
#     ("mypy", "mypy ."),
#     ("pytest", "pytest"),
# )

# implement_checks: shell commands run after each implement phase.
# implement_checks = (
#     "ruff check --fix",
#     "ruff format --check",
#     "mypy .",
#     "pytest",
# )

# Stage-level model/effort overrides — use Claude model IDs and effort levels.
# from pycastle.config import StageOverride
# plan_override = StageOverride(model="claude-opus-4-7", effort="high")
# implement_override = StageOverride(model="claude-sonnet-4-6", effort="normal")
# review_override = StageOverride(model="claude-sonnet-4-6", effort="normal")
# merge_override = StageOverride(model="claude-haiku-4-5-20251001", effort="low")
