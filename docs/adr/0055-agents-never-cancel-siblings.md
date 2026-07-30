# Agents never cancel siblings

ADR 0054 removed `CancellationToken.cancel()` from the `UsageLimited` and non-transient `ProviderUnavailable` paths, letting sibling agents run to their own usage limit. The same reasoning applies to every other error path: a failure in one agent's subprocess tells us nothing reliable about whether its siblings can still make progress.

The remaining `token.cancel()` calls after ADR 0054 were:

- `AgentCredentialFailureError` — a pre-invocation credential check failed (e.g. auth.json missing). Siblings that have already started are unaffected; siblings that haven't started yet will fail their own credential check independently.
- `HardAgentError` — a hard subprocess-level crash (Docker, protocol error, etc.). The crash is specific to that agent's container; sibling containers are not predictably affected.
- `ProviderUnavailable(TRANSIENT_API_ERROR)` — a temporary upstream outage. The error is transient by definition; cancelling siblings aggressively aborts in-flight work that may well have succeeded.
- `ModelNotAvailable` — the requested model is unavailable. `service.mark_model_restricted(model)` is already called before this point, so any sibling that hasn't started yet will see `not service.is_available()` at the early guard and bail out cleanly without Docker setup. Running siblings are unaffected by the model restriction.

All four `token.cancel()` calls are removed. The `CancellationToken` shared across `implement_phase` now serves only as an external interrupt signal (user cancellation); it is never set by an agent's own error outcome.

## Considered Options

- **Keep `token.cancel()` for hard errors as a fail-fast signal.** Rejected: the stale-continuation risk (sibling gets a `Cancelled` outcome mid-session, writes a session ID that Claude won't honour on resume) outweighs the modest Docker setup savings. Each error type already has a correct propagation path that doesn't need the token.
- **Add a `service.mark_unavailable()` replacement for `HardAgentError`.** Rejected: hard errors are not reliably systemic; adding a service-level signal here would stop siblings that could otherwise succeed.
- **Remove all calls — chosen.** Each error type propagates correctly without the token: `AgentCredentialFailureError` and `HardAgentError` are caught and routed at the iteration boundary; `TransientAgentError` produces `Continue()` which retries the iteration; `ModelNotAvailable` is blocked at the early guard via `mark_model_restricted`.

## Consequences

- `token.cancel()` removed from: `AgentCredentialFailureError`, `HardAgentError`, `ProviderUnavailable(TRANSIENT_API_ERROR)`, `ModelNotAvailable`.
- No new "don't start" signals added — `mark_model_restricted` + the existing `not service.is_available()` early guard already covers the `ModelNotAvailable` case.
- Eliminates the remaining class of stale-continuation bugs where a sibling's `token.cancel()` interrupted a Claude session that had started but taken zero turns, leaving a session ID in `_continuation` that Claude no longer recognises on the next resume attempt.
