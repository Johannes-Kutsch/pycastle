# Numbered credential pool per `.env`-credentialed service

> **Consolidates the former dual-Claude-account failover ADR.** Claude auth is OAuth-token only (`claude setup-token`), secrets live in `.env` only, and failover happens at per-agent-run granularity — a 429 marks the credential exhausted, preserves the worktree, and restarts on the next available credential. Those principles, first set for a two-account Claude pool, are subsumed and generalized here.

Both `.env`-credentialed services (Claude, OpenCode) accept an unlimited, priority-ordered pool of interchangeable credentials via numbered suffix keys — `CLAUDE_CODE_OAUTH_TOKEN_2`, `_3`, …; `OPENCODE_GO_API_KEY_2`, `_3`, … — rotated on exhaustion exactly like the earlier two-account Claude pool. The bare key (`CLAUDE_CODE_OAUTH_TOKEN`, `OPENCODE_GO_API_KEY`) is slot 1; lower number = higher priority and is used first. This generalizes the earlier two-account Claude pool to every `.env`-credentialed service and to N credentials.

## Scope

- **In:** Claude and OpenCode — services whose credential is an `.env` value.
- **Out:** Codex — its credential is host-side `~/.codex/auth.json` (ADR 0017), not an `.env` value, so it cannot participate without a fundamentally different mechanism. Multi-account Codex is a separate, larger piece of work.

## Decision detail

- **Representation:** numbered suffixes only for additional credentials. Bare key ≡ slot 1; `_2`, `_3`, … follow. No delimited single-value list, no `_TERTIARY`-style word suffixes.
- **Ordering:** uniform "first listed is used first" — bare/slot 1 is the highest-priority credential. The pool picks the first non-exhausted, non-retired credential top-to-bottom.
- **Rotation triggers:** uniform retire-and-rotate on **both** temporary exhaustion (429 with a reset time → comes back at reset) **and** permanent credential failure (e.g. OpenCode `401 invalid api key`, Claude `403` subscription-access denial → retired for the run). One dead credential must not kill the run. Only when every credential for a service is exhausted-or-retired does the service go unavailable and the cross-service stage-priority chain (ADR 0025) take over; if nothing is left anywhere, the credential failure is surfaced and the run stops.
- **Backward compatibility:** the bare key keeps working unchanged, so single-credential `.env` files are untouched. The `_SECONDARY` word-suffix is **dropped** — dual-Claude-account users migrate `CLAUDE_CODE_OAUTH_TOKEN_SECONDARY` → `CLAUDE_CODE_OAUTH_TOKEN_2`.
- **Conflict:** setting both the bare key and its `_1` form (both claiming slot 1) is a hard config error at startup; ambiguous priority is never guessed.
- **init:** the wizard stays single-key (prompts for the first credential per service); additional numbered credentials are a documented optional manual `.env` edit, the same posture the old `_SECONDARY` key held.

## Considered Options

- **Keep two-account Claude pool, single OpenCode key (status quo).** Rejected: leaves paid OpenCode/secondary subscriptions idle the moment one credential rate-limits, and gives no failover for a fat-fingered key.
- **Delimited single value** (`OPENCODE_GO_API_KEY=k1,k2,k3`). Rejected: OAuth tokens and keys are long opaque strings; comma-packing them on one line is error-prone and unreadable.
- **`_SECONDARY`/`_TERTIARY` word suffixes.** Rejected: does not scale past three and obscures priority.
- **Rotate only on 429, hard-stop on a bad key.** Rejected: inconsistent with the pool's premise — a pool of interchangeable credentials should tolerate one being dead; surfaced loudly only when all are gone.
- **Preserve the secondary-first Claude order.** Rejected: "top to bottom = first used first" is the intuitive reading; treating the bare key as anything but slot 1 is surprising. The flip is cosmetically observable (which account's quota burns first) but behaviorally harmless.

## Consequences

- Supersedes the earlier rejection of the "N-token list" option and the secondary-preferred ordering; OAuth-only auth, env-only secrets, and per-agent-run failover granularity carry over unchanged (see the consolidation note above).
- `KNOWN_CREDENTIAL_ENV_KEYS` can no longer be a fixed tuple — credential collection and the `.env` merge become prefix-aware for the numbered keys.
- OpenCode gains a credential pool with the same `is_available`/`next_wake_time`/`mark_exhausted` exhaustion model the Claude account pool already exposes; a bad OpenCode key now retires rather than aborting the run when alternatives exist.
- Operator-facing status/exhaustion messages identify credentials by slot (e.g. "account 2") rather than "primary"/"secondary".
