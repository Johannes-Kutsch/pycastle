# Pluggable agent services with per-stage fallback

Pycastle hard-coded the Claude CLI in `ContainerRunner` and inlined claude-specific concerns throughout (`CLAUDE_CODE_OAUTH_TOKEN`, stream-json parsing, `--session-id` resume, `AccountPool`). We introduce an `AgentService` abstraction at the **streaming-execution seam**: service owns command construction, env injection, wire-format parsing, and its own resume contract. `ContainerRunner` keeps cross-cutting concerns (prompt-file write, log persistence, idle-timeout, `on_turn`/`on_tokens` callbacks). Services are declared by name in config; each stage names a preferred service plus an optional `(service, model, effort)` fallback. Service-internal account pooling becomes a private detail of `ClaudeService`. Cross-service mid-stage handoff works via WIP commits — on `UsageLimitError` / `AgentTimeoutError` the orchestrator commits dirty changes as `WIP: <role> #N - interrupted` with the agent's last assistant turn as body, then unwinds; the next dispatch's prompt is augmented with "this branch carries WIP, inspect and continue" only when WIP commits are present *and* the picked service has no resume state for this worktree.

## Considered Options

- **CLI-builder seam (service returns command + env).** Rejected: forces every future service to mimic claude's stream-json and `--session-id` semantics.
- **Full-agent-run seam (service owns image, auth, returns `AgentOutput`).** Rejected: duplicates docker/session/log/timeout machinery per service; makes ADR 0006 hard to keep uniform.
- **Streaming-execution seam — chosen.** Service yields normalized turn events (`AssistantTurn(text)`, `Tokens(count)`, `Result(text)`, `UsageLimit(reset_time)`) into a shared coordinator.
- **Rich `ParsedTurn` vocabulary (`ToolUse`, `ToolResult`, `Reasoning`).** Rejected: invents surface for nonexistent consumers; expand when needed.
- **Raw-event opaque blob + per-service extractor.** Rejected: equivalent to 4-event vocabulary with types off.
- **Shared on-disk resume shape, parameterized env-var.** Rejected: silently downgrades resume-less services to always-FRESH.
- **Cross-service handoff via transcript replay.** Rejected: invents a cross-service transcript format ahead of need; pays token cost every handoff.
- **Sticky-service (no mid-stage switch).** Rejected: defeats failover — sleeping when codex is idle is exactly what we want to avoid.
- **Discard A's uncommitted changes on cross-service FRESH.** Rejected: loses near-complete implement work — the scenario session-resume exists for.
- **Always-on auto-commits during run (agent-driven).** Rejected: every agent pays prompt cost; granularity decided by model not orchestrator.
- **Global ordered service-priority list, per-stage `service=` shifts start.** Rejected: fallback often wants a different `(service, model, effort)` triple — codex's best implement model differs from review.
- **`start_sha` file as stage-in-progress marker.** Rejected: squash anchor is derivable from git history (`git log` minus `^WIP:` subjects).
- **Per-stage `services=[...]` list.** Rejected: no expressive gain over nested `fallback=`; encourages baroque trees.

## Consequences

- **`AgentService` protocol** at the streaming-execution seam. Methods: `name`, `is_available(now)`, `next_wake_time()`, `mark_exhausted(reset_time)`, `state_dir_relpath(role, namespace) -> str | None`, `is_resumable(state_dir: Path) -> bool`, `build_env(state_dir_container_path) -> dict[str, str]`, async `run(...)` yielding 4-event `ParsedTurn` stream. Services under `src/pycastle/services/`.
- **Worktree marker layout:**
  - `.pycastle-session/<role>/[<namespace>/]` — presence flags "stage started"; empty = done, non-empty = in progress.
  - `.pycastle-session/<role>/[<namespace>/]<service>/` — per-service resume state.
  - No `start_sha` file. Stage-completion squash walks back from HEAD skipping `^WIP: <role> #N -`.
- **`RoleSession` splits along two axes.** Stage-completion (service-agnostic) stays: `is_done()` / `mark_done()` / `start_fresh()`. Service resume state moves into the service via `state_dir_relpath` + `is_resumable`. `is_stage_done_for(worktree, role)` stays as pure-filesystem check.
- **Conditional WIP-aware prompt clause.** Fired only when (a) WIP commits exist on the branch AND (b) picked service has no resume state in this worktree. Same-service resume gets neither clause nor narrative — claude already has the conversation.
- **WIP-commit lifecycle.** On `UsageLimitError` / `AgentTimeoutError` before teardown: `git add -A && git commit -m "WIP: <role> #N - interrupted"` with last assistant turn as body. `AgentFailedError` and unexpected exceptions don't WIP-commit.
- **Squash-at-stage-end.** Walk back from HEAD skipping WIP subjects to find squash anchor; `git reset --soft <anchor> && git commit -m "Implement #N - <msg>"`.
- **FRESH-start hygiene.** Every FRESH dispatch runs `git restore --staged --worktree . && git clean -fd` against the worktree.
- **Config shape.** `StageOverride` gains `service: str` and `fallback: StageOverride | None`. Top-level `services: dict[str, AgentService]` and `default_service: str`. Empty `service=""` resolves to `default_service`.
- **Dispatch loop.** Ask `services[stage.service].is_available(now)` → use if yes. Else `stage.fallback`. Else sleep until `min(next_wake_time across involved services)`. Snap-back automatic.
- **Service-internal failover stays internal.** Claude's two-OAuth-token pool moves from `AccountPool` (today `main.py`) to a private detail of `ClaudeService`. `ClaudeService.is_available()` True iff any internal account non-exhausted.
- **`AbortedUsageLimit` arm** consults the registry of services configured for the current stage; sleeps only when both primary and fallback unavailable.
- **`process_stream` and `_HANDLERS` survive unchanged.** 4-event vocabulary streams `AssistantTurn(text)` into existing role handlers. `_check_usage_limit` regex and `_extract_turn` JSON path become claude-private inside `ClaudeService`.
- **ADR 0006 amendment.** Session resume scoped to picked service. Non-typed Resume retry stays per-service. `--resume <SESSION_UUID>` and `CLAUDE_CONFIG_DIR` become claude-private.
- **ADR 0007 amendment.** Stage-done signal (empty role dir) preserved. Contents shift from claude session files to `<service>/` subdirs.
- **ADR 0005 supersession (partial).** `AccountPool` moves into `ClaudeService` internals. Dual-token failover preserved byte-for-byte at user-visible level.
- `pycastle init` env collection per-service revisited in follow-up. v1 ships `ClaudeService` only plus the seam; codex lands separately (ADR 0020).
