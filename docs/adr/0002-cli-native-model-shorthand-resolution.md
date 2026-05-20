# CLI-native model shorthand resolution

Model shorthands (e.g. `sonnet`) pass directly to the Claude CLI at stage execution; `load_config` stays pure (file I/O only) and stores the string as-is.

## Reasons

- **Hidden interface cost.** Resolving at load time would turn `load_config` into a subprocess caller — callers and tests must mock `ClaudeService` to avoid hitting the CLI.
- **CLI supports it.** `claude --model sonnet` works natively.
- **Locality of validation.** Bad model strings surface as CLI errors at the point of use, with stage/run context.
- **Testability.** Pure `load_config` tests with plain `Config` comparisons, no mocks.

## Consequences

- `Config.<stage>_override.model` may hold a shorthand or full model ID — callers can't distinguish by type.
- Bad model strings surface at first stage run, not at config load.
- Effort validation (set-membership) stays inline in `load_config`; `validator.py`'s `_fetch_models` / `_resolve_shorthand` machinery is removed.
- `load_config` no longer accepts a `claude_service` argument.
