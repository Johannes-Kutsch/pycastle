# Claude subscription denial is permanent account exhaustion

Claude Code can return a `403` result envelope whose message says the organization has disabled Claude subscription access for Claude Code and suggests using an Anthropic API key or asking an admin to enable access. Pycastle does not support an Anthropic API-key fallback for Claude, so this specific denial is treated as a permanent exhaustion signal for the active Claude account rather than as a normal hard API failure.

The key distinction is scope. This is not a blanket `403` policy. Other Claude authorization failures remain errors. Only the exact subscription-access denial text is mapped to permanent account exhaustion.

## Considered Options

- **Treat every `403` as permanent account exhaustion.** Rejected: Claude `403` is a generic permission error, so blanket exhaustion would hide unrelated authorization problems and could incorrectly retire a healthy account.
- **Treat the exact subscription-access denial text as permanent exhaustion.** Chosen: the message is specific, stable enough to match literally, and expresses a product-level restriction that should not be retried.
- **Add an Anthropic API-key fallback.** Rejected: that would introduce a second Claude auth model and change the product boundary instead of surfacing the existing restriction clearly.

## Consequences

- The exact subscription-access denial result is handled like account exhaustion, not like a transient API error.
- The active Claude account is marked unavailable for the remainder of the process lifetime.
- If a standby Claude account is available, pycastle falls through to it immediately instead of sleeping.
- The user-visible diagnostic names the service and the exhausted account label so it is clear which credential stopped working.
- The message stays distinct from usage-limit output; this is a permanent access problem, not a resettable rate limit.
