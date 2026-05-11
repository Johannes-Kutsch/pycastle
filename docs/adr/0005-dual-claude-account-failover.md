# Dual Claude account failover via OAuth token pool

Pycastle previously supported three Claude authentication paths in parallel: `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, and `CLAUDE_ACCOUNT_JSON`. When a Pro/Max subscription hit its rate limit the orchestrator slept until the reported `reset_time + 2 min`, even when a second subscription was available. All Claude auth collapses onto OAuth tokens (`claude setup-token`-generated), supporting a primary plus optional secondary account. An in-memory `AccountPool` picks a non-exhausted token at each agent spawn and only sleeps when every account is exhausted, until the earliest pool wake-time.

## Considered Options

- **Status quo (single account, sleep on limit).** Rejected: leaves a second paid subscription idle for hours per day.
- **Two `~/.claude.json` files on disk, secondary path configurable.** Rejected after verification that `setup-token` produces valid long-lived OAuth tokens for Pro accounts. Env-only auth keeps secrets in one place (`.env`), removes the Windows path bug surface (#467), and lets the AccountPool work over uniform `(name, token)` tuples.
- **N-token list (`CLAUDE_CODE_OAUTH_TOKEN_1..N`).** Rejected: the concrete use case is exactly two accounts, numbered keys hide priority semantics, and migration churns existing users. Two named keys (primary unchanged, secondary additive) preserves the existing `.env` exactly.
- **Mid-flight container token swap.** Rejected: Docker doesn't support mutating `container_env` of a running container. The existing worktree preservation + restart mechanism is cleaner.
- **Keep `ANTHROPIC_API_KEY` as an orthogonal third path.** Rejected: API-key auth has no usage-limit semantics (pay-per-token), so it doesn't fit the pool model, and supporting it forces a carve-out branch in every credential code path.

## Consequences

- `~/.claude.json` is no longer read by pycastle; users who authenticated only via `claude login` must run `claude setup-token` once and paste the result into `.env`. One-time migration step.
- `ANTHROPIC_API_KEY` is removed from `_ENV_KEYS`, the init template, and all credential plumbing. Pycastle is committed to subscription auth only.
- `AccountPool` is constructed in `main.py` alongside `_load_env` and passed to `AgentRunner.__init__`. It is *not* threaded through `Deps` — the iteration layer has no access to credentials.
- The existing `UsageLimitError` → `AbortedUsageLimit` plumbing is reused unchanged; only the orchestrator's match arm at `orchestrator.py:182` is gated on `pool.has_available()` before sleeping. Single-token users see no change.
- Failover is per-agent-run granularity. Each parallel agent that 429s calls `mark_exhausted` on the same token (idempotent) and unwinds via the standard preserve-and-restart flow.
- `_inject_claude_credentials` and the `CLAUDE_ACCOUNT_JSON` env-filter in `_build_session` are deleted.
- `pycastle init` still prompts for a single OAuth token. The secondary is documented as an optional `.env` addition.
