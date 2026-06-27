from pycastle import StageOverride

# --- Behaviour ---
# max_iterations = 10
# max_parallel = 1
# worktree_timeout = 30
# idle_timeout = 300
# auto_push = True
# timeout_retries = 1
# diagnose_on_failure = True
# Minimum unknown-reset duration (hours) for usage-limit exhaustion when reset time is not explicit.
# The wake estimate is aligned to the next hour boundary plus 2 minutes and may be further in the future.
# claude_minimum_unknown_reset_duration_hours = 0.0
# codex_minimum_unknown_reset_duration_hours = 0.0
# opencode_minimum_unknown_reset_duration_hours = 1.0

# --- Docker ---
# Local-only build artifact name used by `pycastle build`.
# Rejected in global config.py.
# Defaults to a sanitised CWD name when left empty.
# docker_image_name = ""

# --- Labels ---
# bug_label = "bug"
# issue_label = "ready-for-agent"
# hitl_label = "ready-for-human"
# enhancement_label = "enhancement"
# needs_triage_label = "needs-triage"
# needs_info_label = "needs-info"
# wontfix_label = "wontfix"
# refactor_slice_label = "refactor-slice"
# behavior_slice_label = "behavior-slice"
# docs_slice_label = "docs-slice"
# needs_slice_type_label = "needs-slice-type"

# --- Logging ---
# In local config, logs_dir is used directly.
# In global config, logs_dir is the parent directory for per-project logs.
# logs_dir = Path("pycastle/logs")

# --- Preflight checks ---
# Run by pycastle before agent work; format: (name, command).
# preflight_checks = (
#     ("ruff", "ruff check ."),
#     ("mypy", "mypy ."),
#     ("pytest", "pytest"),
# )

# --- Host checks ---
# Run by `pycastle check` on the current OS; format: (name, command).
# host_checks = (
#     ("pytest", "pytest"),
# )

# --- Implement checks ---
# injected via prompt - these commands appear in the agent's FEEDBACK LOOPS
# section, they are not run directly by pycastle config.
# implement_checks = (
#     "ruff check --fix",
#     "ruff format --check",
#     "mypy .",
#     "pytest",
# )

# --- Improve ---
# Default improve mode used when --improve is not passed on the CLI.
# Options: "until_sleep", "endless", or None.
# improve_mode = None

# Maximum number of improve-agent dispatches per run.
# improve_max = None

# --- Stage overrides ---
# Claude model shorthands: haiku, sonnet, opus
# Codex model names: gpt-5.5, gpt-5.4, gpt-5.4-mini, gpt-5.3-codex, gpt-5.3-codex-spark, gpt-5.2
# OpenCode Go model ids: bare ids in config; pycastle passes opencode-go/<id> to the OpenCode CLI.
# Supported: deepseek-v4-flash, deepseek-v4-pro, glm-5.2, glm-5.1, kimi-k2.7-code, kimi-k2.6, mimo-v2.5-pro, mimo-v2.5, minimax-m2.7, minimax-m3, qwen3.6-plus, qwen3.7-max, qwen3.7-plus
# Claude effort values: low, medium, high, xhigh, max
# Codex effort values: low, medium, high, xhigh
# OpenCode effort values: medium
# Lower-cost OpenCode planning alternative:
# plan_override = StageOverride(service="opencode", model="deepseek-v4-flash", effort="medium")
# Opt-in example:
# opencode_review_override = StageOverride(service="opencode", model="kimi-k2.6", effort="medium")
# opencode_implement_override = StageOverride(
#     service="opencode",
#     model="kimi-k2.6",
#     effort="medium",
#     fallback=StageOverride(service="codex", model="gpt-5.3-codex-spark", effort="high"),
# )
plan_override = StageOverride(
    service="opencode",
    model="kimi-k2.6",
    effort="medium",
    fallback=StageOverride(
        service="codex",
        model="gpt-5.4-mini",
        effort="low",
        fallback=StageOverride(service="claude", model="haiku", effort="low"),
    ),
)
implement_override = StageOverride(
    service="codex",
    model="gpt-5.3-codex-spark",
    effort="high",
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
