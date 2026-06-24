# Three-bucket Claude API error handling at the protocol parsing layer

`claude_service` classifies any `is_error: true` result envelope into three buckets at the parsing layer, uniform across every `AgentRole`:

- **429** → existing `UsageLimitError` (account-specific; `mark_exhausted`; sleep to reset).
- **5xx or `is_error: true` with no status** → `TransientAgentError`. Worktree preserved; not-yet-started siblings cancelled; running siblings finish; no `mark_exhausted`. Next iteration re-spawns via in-flight detection.
- **4xx other than 429** → `HardAgentError`, except credential/account-access failures routed by ADR 0039. Worktree preserved; siblings cancelled; one bug filed via `auto_file_issue` (ADR 0022). Exits non-zero after current iteration.

Trigger: issue #831 — Reviewer hit 529 Overloaded, CLI emitted `is_error: true` result, pre-existing detector only matched 429, so the line was parsed as normal `Result`. Orchestrator composed a synthetic commit and the broken review was treated as complete.

## Considered Options

- **Patch 529 only.** Rejected: leaves same bug latent for all other 5xx/4xx.
- **Reuse `UsageLimitError` for 5xx.** Rejected: conflates account exhaustion with server-wide outage.
- **One unified `AgentApiError` branching on status field.** Rejected: 5xx and 4xx have different policies; encoding in hierarchy makes handler trivial.
- **Special-case subscription-access denial as hard error.** Rejected: now routed by ADR 0039's shared credential-failure policy.

## Consequences

- No `is_error: true` line is ever yielded as a `Result`.
- Broadened preservation rule recognises both new exceptions alongside `UsageLimitError`.
- Iteration boundary gains two new catches per ADR 0008 pattern: transient → next iteration no sleep; hard → auto-file + exit non-zero.
- Stage-done sentinel never flips — protocol raises before `Result` reaches a role handler.
- Scope is Claude-only; Codex has its own parser.
