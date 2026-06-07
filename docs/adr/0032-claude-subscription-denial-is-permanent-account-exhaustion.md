# Claude subscription denial is permanent account exhaustion

Superseded by ADR 0043.

Claude Code can return a `403` result envelope whose message says the organization has disabled Claude subscription access for Claude Code and suggests using an Anthropic API key or asking an admin to enable access. Pycastle does not support an Anthropic API-key fallback for Claude, so this specific denial is treated as a permanent exhaustion signal for the active Claude account rather than as a normal hard API failure.

The key distinction is scope. This is not a blanket `403` policy. Other Claude authorization failures remain errors. Only a `403` whose result text contains the stable phrase "disabled Claude subscription access for Claude Code" is mapped to permanent account exhaustion; surrounding remediation text and punctuation may vary without changing the classification.

## Considered Options

- **Treat every `403` as permanent account exhaustion.** Rejected: Claude `403` is a generic permission error, so blanket exhaustion would hide unrelated authorization problems and could incorrectly retire a healthy account.
- **Treat the subscription-access denial phrase as permanent exhaustion.** Chosen: the meaning-bearing phrase is specific and expresses a product-level restriction that should not be retried, while the surrounding remediation text has already varied in real envelopes.
- **Add an Anthropic API-key fallback.** Rejected: that would introduce a second Claude auth model and change the product boundary instead of surfacing the existing restriction clearly.

## Consequences

- The subscription-access denial result is handled like account exhaustion, not like a transient API error.
- The active Claude account is marked unavailable for the remainder of the process lifetime.
- Permanent exhaustion behaves like a sleeping account that never wakes during the process: it removes only the failing Claude account/service candidate, while the normal stage fallback chain decides whether another account or service can continue. Existing usage-limit handling still applies inside the current iteration: not-yet-started sibling agents are cancelled, already-running siblings may finish, and the boundary then decides whether fallback can continue.
- If a standby Claude account is available, pycastle falls through to it immediately instead of sleeping.
- If no standby account or service fallback is available for the current stage and the only remaining wake time is the permanent exhaustion sentinel, pycastle prints a warning and stops the current run cleanly; it does not sleep until the permanent exhaustion sentinel and does not auto-file a pycastle bug.
- The user-visible diagnostic names the service and account label, says the account is retired for this run and will be retried on the next run, and includes the parsed Claude denial message.
- The message stays distinct from usage-limit output; this is a permanent access problem, not a resettable rate limit.
