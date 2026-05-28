# CLI-native model shorthand resolution

Model shorthands (e.g. `sonnet`) pass directly to the selected service CLI at stage execution; `load_config` stays pure (file I/O only) and stores the string as-is. `pycastle run` validates each non-empty model shorthand against the selected `AgentService.valid_models()` allowlist before credential checks, image builds, or agent dispatch.

## Reasons

- **Hidden interface cost.** Resolving at load time would turn `load_config` into a subprocess caller — callers and tests must mock `ClaudeService` to avoid hitting the CLI.
- **CLI supports it.** `claude --model sonnet` works natively.
- **Locality of validation.** Bad model strings surface during `pycastle run` startup, with stage/run context, before slower credential and image-build work.
- **Testability.** Pure `load_config` tests with plain `Config` comparisons, no mocks.

## Consequences

- `Config.<stage>_override.model` may hold the empty string (CLI default) or a known service-specific shorthand.
- Bad non-empty model strings surface during `pycastle run` startup, not at config load.
- Effort validation (set-membership) stays inline in `load_config`; `validator.py`'s `_fetch_models` / `_resolve_shorthand` machinery is removed.
- `load_config` no longer accepts a `claude_service` argument.
