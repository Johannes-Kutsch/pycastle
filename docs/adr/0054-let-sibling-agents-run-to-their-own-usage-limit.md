# Let sibling agents run to their own usage-limit instead of cancelling them

When one parallel agent inside `implement_phase` hits a usage limit (`UsageLimited`, `ProviderUnavailable`, or OpenCode `TimedOut` / `AgentCredentialFailureError`), the previous code called `CancellationToken.cancel()`. This propagated a runtime cancel signal to every other agent sharing the token, forcing them to stop mid-run and surface as `"cancelled"` even though their own session budget was untouched.

The cost is real: each agent runs in its own Claude Code session with an independent per-session message budget. A sibling that is mid-review when the cancel arrives loses in-flight generation that Claude was already producing — work that would have been "free" because the budget is consumed at the boundary of the *next* request, not mid-stream. If the sibling was near the end of its review it would have completed and written a commit; cancelling it forces a full retry on the next wake.

The fix splits the two roles the `CancellationToken` was serving:

- **Block agents that haven't started yet.** When an account is exhausted, `_handle_provider_account_exhaustion` marks the service, so `service.is_available()` returns `False`. The early guard in `_run_with_runtime_client` now raises `UsageLimitError` when `not service.is_available()` (in addition to when `token.is_cancelled`). Agents waiting on the concurrency semaphore are stopped on entry with no wasted Docker setup.
- **Leave running agents alone.** `token.cancel()` is removed from the `UsageLimited`, `ProviderUnavailable` (non-transient), OpenCode `TimedOut`, and OpenCode `AgentCredentialFailureError` paths. Running agents receive no cancel signal and continue until they complete or hit their own limit.

`token.cancel()` is kept for `HardAgentError` and non-OpenCode `AgentCredentialFailureError`, where broken credentials or a hard runtime error make stopping siblings immediately correct.

## Considered Options

- **Status quo: cancel all siblings on first limit.** Rejected: wastes in-flight generation; the sibling's own session budget is independent and may be nowhere near exhausted.
- **Don't cancel, use `token.is_cancelled` as the "don't start" guard.** Rejected: if we don't cancel the token for usage limits, the early guard never fires, so pending agents attempt Docker setup and immediately fail their first API call.
- **Separate "no-start" token from the runtime cancel signal — chosen.** The existing service-availability predicate (`service.is_available()`) is already the canonical source of account exhaustion truth; routing the early guard through it is the smallest change and correctly handles multi-account pools without new plumbing.

## Consequences

- `_run_with_runtime_client` early guard: `if token.is_cancelled or not service.is_available()`.
- `token.cancel()` removed from: `UsageLimited`, `ProviderUnavailable` (non-transient), OpenCode `TimedOut`, OpenCode `AgentCredentialFailureError`.
- `token.cancel()` kept for: `HardAgentError`, non-OpenCode `AgentCredentialFailureError`.

**Amendment (ar 0.2.9 / ar ADR 0022):** The OpenCode `TimedOut` and OpenCode `AgentCredentialFailureError` branches mentioned above no longer exist in pycastle's runner. Ar 0.2.9 normalizes both signals before pycastle sees them: idle-timeout → `UsageLimited(reset_time=None, is_permanent=False)`; 401 permanent account exhaustion → `UsageLimitError(is_permanent=True)`. The no-cancel policy for those paths is preserved — it now applies through the existing `UsageLimited` handler.
- A cancelled-by-hard-error agent displays `"cancelled"` (interrupted style); an agent that hits its own limit displays `"usage limit reached"` (same as before).
- `implement_phase` result aggregation is unchanged — it already handles mixed outcomes where some issues complete and some hit limits.
