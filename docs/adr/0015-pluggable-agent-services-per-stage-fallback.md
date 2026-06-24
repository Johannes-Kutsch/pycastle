# Pluggable agent services with per-stage fallback

`AgentService` abstraction at the **streaming-execution seam**: service owns command construction, env injection, wire-format parsing, and resume contract. `ContainerRunner` keeps cross-cutting concerns (prompt-file write, log persistence, idle-timeout, callbacks). Each stage names a preferred service plus optional `(service, model, effort)` fallback. Service-internal account pooling is a private detail of `ClaudeService`. Cross-service handoff preserves the dirty worktree as-is with an interrupted-work prompt clause — no WIP commit, no working-tree wipe.

## Considered Options

- **CLI-builder seam.** Rejected: forces every service to mimic claude's stream-json.
- **Full-agent-run seam.** Rejected: duplicates docker/session/log machinery per service.
- **Streaming-execution seam — chosen.** Service yields 4-event `ParsedTurn` (`AssistantTurn`, `Tokens`, `Result`, `UsageLimit`).
- **WIP commits on interruption.** Rejected: commit/squash lifecycle adds complexity without proportional gain. Dirty worktree + prompt clause suffices.
- **Sticky-service (no mid-stage switch).** Rejected: defeats failover.

## Consequences

- **`AgentService` protocol:** `name`, `is_available(now)`, `next_wake_time()`, `mark_exhausted(reset_time)`, `state_dir_relpath(role, namespace)`, `is_resumable(state_dir)`, `build_env(...)`, async `run(...)` yielding `ParsedTurn`.
- **Worktree layout:** `.pycastle-session/<role>/[<namespace>/]` (stage started); `.pycastle-session/<role>/[<namespace>/]<service>/` (per-service resume state).
- **`RoleSession` split:** stage-completion (service-agnostic) stays; service resume moves to service via `state_dir_relpath` + `is_resumable`. `start_fresh()` wipes entire role dir.
- **Interrupted-work prompt clause:** fired when `run_kind == FRESH` and worktree dirty. Agent inspects via `git diff`/`git status`.
- **Config:** `StageOverride` gains `service: str` and `fallback: StageOverride | None`.
- **Dispatch:** try `is_available(now)` → use; else fallback; else sleep to `min(next_wake_time)`. Snap-back automatic.
- `AccountPool` moves into `ClaudeService` internals (amends ADR 0005). Session resume scoped to picked service (amends ADR 0006). Stage-done signal preserved, contents shift to `<service>/` subdirs (amends ADR 0007).
