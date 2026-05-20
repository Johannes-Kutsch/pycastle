# Dual Claude account failover via OAuth token pool

All Claude auth collapses onto OAuth tokens (`claude setup-token`-generated): primary plus optional secondary. An in-memory pool picks a non-exhausted token per agent spawn and sleeps only when every account is exhausted, until the earliest wake-time. Drops `ANTHROPIC_API_KEY` and `CLAUDE_ACCOUNT_JSON`. Previously, a Pro/Max rate-limit slept until `reset_time + 2 min` even with a second subscription available.

## Considered Options

- **Single account, sleep on limit.** Rejected: leaves a paid subscription idle for hours/day.
- **Two `~/.claude.json` files on disk.** Rejected: `setup-token` produces valid long-lived tokens for Pro accounts; env-only keeps secrets in `.env`, removes Windows path bugs (#467), uniform `(name, token)` tuples.
- **N-token list `CLAUDE_CODE_OAUTH_TOKEN_1..N`.** Rejected: concrete use case is two accounts; numbered keys hide priority; churns existing `.env`.
- **Mid-flight container token swap.** Rejected: Docker can't mutate `container_env` on a running container; worktree preserve + restart is cleaner.
- **Keep `ANTHROPIC_API_KEY` as third path.** Rejected: API-key has no usage-limit semantics (pay-per-token); doesn't fit pool model; forces carve-out branches everywhere.

## Consequences

- `~/.claude.json` no longer read; one-time migration: run `claude setup-token`, paste into `.env`.
- `ANTHROPIC_API_KEY` removed from `_ENV_KEYS`, init template, credential plumbing. Subscription auth only.
- **Amended by #691** (per ADR 0015): `AccountPool` is no longer orchestrator-level; logic lives inside `ClaudeService` as private `_AccountPool`. `main.py` builds `dict[str, AgentService]` service registry passed to `orchestrator.run()`. `AgentRunner` receives `ClaudeService` via `service` parameter; token pick/exhaustion are internal.
- `AgentService` protocol gains `is_available(now)`, `next_wake_time()`, `mark_exhausted(reset_time)`. `AbortedUsageLimit` arm consults `service.is_available(now)`; sleeps to `min(next_wake_time())` only when all services unavailable.
- `UsageLimitError → AbortedUsageLimit` plumbing reused unchanged. Single-token users see no change.
- Failover at per-agent-run granularity; 429 → `mark_exhausted(reset_time)` + standard preserve-and-restart.
- `_inject_claude_credentials` and the `CLAUDE_ACCOUNT_JSON` env-filter deleted.
- `pycastle init` prompts for one OAuth token; secondary is a documented optional `.env` addition.
