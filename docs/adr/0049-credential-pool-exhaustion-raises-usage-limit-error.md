# Credential pool exhaustion raises UsageLimitError from build_env

When `CredentialPool.pick()` raises inside `ClaudeService.build_env()` because every credential slot is exhausted, the raw `RuntimeError` propagated through the full call stack. `implement_phase` collects exceptions via `asyncio.gather(return_exceptions=True)` and only re-raises known typed exceptions (`AgentFailedError`, `HardAgentError`, `TransientAgentError`, `ModelNotAvailableError`, `UsageLimitError`). A plain `RuntimeError` fell through to the generic `isinstance(result, Exception)` branch, was logged, and was added to `errors`. With no completions and `usage_limit_hit=False`, `_run_implement_and_merge` returned `Continue()`, causing the orchestrator to loop endlessly against accounts that would never become available mid-loop.

## Decision

`ClaudeService.build_env()` catches `RuntimeError` from `pick()` and raises `UsageLimitError` instead:

- If the pool has at least one finite wake time (`earliest_wake_time()` succeeds) → `UsageLimitError(reset_time=wake_time, provider="claude")` → `TemporaryUsageLimit` path → sleep until the earliest slot wakes.
- If every slot is permanently exhausted (`earliest_wake_time()` raises) → `UsageLimitError(is_permanent=True, provider="claude")` → `PermanentlyExhausted` path → stop cleanly.

This converts pre-run credential exhaustion into the same typed signal as runtime exhaustion surfaced by ar, routing through the existing `AbortedUsageLimit` → `decide_usage_limit_continuation` → sleep/stop machinery.

## Considered options

- **Catch at `implement_phase` level.** Rejected: requires `implement_phase` to know about `RuntimeError` message text from a separate module, creating a fragile cross-layer dependency.
- **New `CredentialPoolExhaustedError` type.** Rejected: adds a new exception type that also needs a handler at every boundary; `UsageLimitError` already carries the full semantic and is already handled everywhere that matters.

## Consequences

- `ClaudeService.build_env()` imports `UsageLimitError` from `pycastle.errors`.
- Pre-run pool exhaustion now sleeps-and-retries or stops, matching runtime exhaustion behaviour.
- The `TemporaryUsageLimit` and `PermanentlyExhausted` glossary entries in CONTEXT.md gain this additional production path.
