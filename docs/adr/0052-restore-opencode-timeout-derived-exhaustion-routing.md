# Restore OpenCode timeout-derived exhaustion routing under ar

> **Restores:** ADR 0043 (OpenCode timeout exhaustion uses unknown-reset usage-limit path), which was incorrectly dropped by ADR 0049.

ADR 0049 replaced pycastle's provider layer with `ruhken-agent-runtime` and noted that OpenCode idle-timeout retries were superseded by `TimedOut` outcome + Continuation resume. That statement was wrong: the OpenCode-specific transform from idle-timeout to usage-limit exhaustion was a distinct routing decision that should have survived the ar migration. When OpenCode hits its quota it goes silent; ar fires the idle timeout and returns `TimedOut`. Resuming the session immediately hits the same wall, so retrying is counter-productive. The account should be retired and the credential pool or fallback chain should take over — exactly as it does for structured usage-limit events.

This ADR restores the intent of ADR 0043 under the ar execution model.

## Decision

When ar returns a `TimedOut` outcome for an OpenCode run, pycastle skips the `timeout_retries` resume loop and instead raises `UsageLimitError(provider="opencode", raw_message=...)` on the first timeout. The existing `_handle_provider_account_exhaustion` path retires the account, rotates to the next available OpenCode credential, or falls back to the next stage service in the priority chain if the pool is exhausted.

Claude and Codex are unaffected. Their `TimedOut` outcomes continue to trigger the `timeout_retries` resume loop because idle hangs on those services are genuine silence events (not quota exhaustion).

`opencode_minimum_unknown_reset_duration_hours` is raised from `0.0` to `1.0` so that an unknown-reset OpenCode exhaustion waits at least one hour before the account is considered available again, preventing a tight retry loop against an exhausted account.

## Considered options

- **Keep `TimedOut` → Continuation resume for all services.** Rejected: for OpenCode, resuming immediately against a quota-exhausted account wastes the retry budget and leaves the run stalled until all retries are exhausted, only then producing `AbortedTimeout` with no account rotation.
- **Add a separate OpenCode quota-detection signal before timeout fires.** Rejected: OpenCode does not emit a structured quota event; detecting silence earlier than the idle timeout introduces complexity with no reliable trigger.

## Consequences

- ADR 0043's superseded status note is revised: the `TimedOut` + Continuation resume statement in ADR 0049 did not replace the OpenCode timeout-to-usage-limit transform — it only replaced the *retry mechanism*. The routing decision restated here stands independently.
- The `opencode_minimum_unknown_reset_duration_hours` config default is now `1.0`; existing configs that explicitly set `0.0` retain the prior next-hour heuristic behaviour unchanged.
- Status rows paint `"interrupted"` on OpenCode quota-timeout (inherited from the usage-limit path) rather than `"failed"` (which would have appeared via `AbortedTimeout`).
