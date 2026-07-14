# Codex authentication via per-role workspace-local `auth.json`

Codex CLI requires ChatGPT OAuth (`codex login` → `~/.codex/auth.json`); can't use `OPENAI_API_KEY` for subscription billing. Pycastle seeds per-role copies from the host file. Per-role copies diverge after first refresh — each role becomes its own refresh-token lineage. `refresh_token_reused` only fires when the *same* token is reused, not independently-rotated descendants.

## Considered Options

- **`OPENAI_API_KEY`.** Rejected: can't route subscription billing.
- **Single shared bind-mount.** Rejected: N containers race on refresh.
- **Per-role workspace-local, seeded at init and lazily at runtime — chosen.** Each role at `.pycastle-session/<role>/[<namespace>/]codex/auth.json`. Runtime seed required for transient worktrees.
- **Copy entire `~/.codex/`.** Rejected: pycastle owns model/effort/role policy; host config invites footguns.

## Consequences

- **`CodexService`** implementing `AgentService`. Single-account v1.
- **`build_command`:** Fresh: `codex exec [flags] <prompt>`. Resume: `codex exec resume <thread_id> [flags] <prompt>`. `thread_id` from `thread.started` JSONL event, persisted by `AgentRunner`; on Resume, reads from sidecar or recovers from `rollout-*.jsonl`.
- **`build_env`:** `TZ=UTC` (load-bearing: codex renders reset timestamps in process-local timezone) and `CODEX_HOME=<workspace>/.pycastle-session/<role>/[<namespace>/]codex/`.
- **`is_resumable`:** True iff `state_dir/sessions/` contains `rollout-*.jsonl`. Pre-seeded `auth.json` does not count.
- **Sandbox:** Fresh sessions pass `--sandbox danger-full-access`; resumed sessions inherit (resume subcommand does not accept `--sandbox`).
- **Init:** service-selection prompt (`claude/codex/opencode/all`). Codex has no `.env` secret; runtime auth is workspace-local `auth.json`.
- **Host-token staleness:** after first refresh host file may be behind. Missing host auth → hard 401.
- **Wire-format:** JSONL `ThreadEvent`s. No `Result` yield — terminates on `turn.completed`.
