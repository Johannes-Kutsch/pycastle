# CLI-native model shorthand resolution

Model shorthands (e.g. `sonnet`) stay service-specific and are validated before agent dispatch; `load_config` stays pure (file I/O only) and stores the string as-is. For Claude and Codex, the shorthand is also the CLI argument. For OpenCode Go, pycastle config stores bare Go ids such as `deepseek-v4-flash`, while `OpenCodeService` maps the value to the provider-qualified OpenCode CLI ref `opencode-go/deepseek-v4-flash`.

## Reasons

- **Hidden interface cost.** Resolving at load time would turn `load_config` into a subprocess caller — callers and tests must mock `ClaudeService` to avoid hitting the CLI.
- **CLI supports it.** `claude --model sonnet` works natively.
- **Locality of validation.** Bad model strings surface during `pycastle run` startup, with stage/run context, before slower credential and image-build work.
- **Testability.** Pure `load_config` tests with plain `Config` comparisons, no mocks.

## Consequences

- `Config.<stage>_override.model` may hold the empty string (CLI default) or a known service-specific shorthand.
- OpenCode provider-qualified refs are an adapter concern, not user config. Config validation rejects arbitrary OpenCode provider strings while `OpenCodeService` emits the `opencode-go/<id>` CLI shape required by OpenCode.
- Bad non-empty model strings surface during `pycastle run` startup, not at config load.
- Effort validation (set-membership) stays inline in `load_config`; `validator.py`'s `_fetch_models` / `_resolve_shorthand` machinery is removed.
- `load_config` no longer accepts a `claude_service` argument.
