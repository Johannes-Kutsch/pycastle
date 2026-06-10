# Runtime compatibility artifact policy

Issue #1658 resolved the runtime-facing compatibility names that still carry pycastle vocabulary during the `pycastle_agent_runtime` ownership migration.

The runtime package should expose runtime-neutral public vocabulary. Pycastle-named artifacts may remain only as compatibility shims for existing pycastle behaviour and old artifacts, not as generic runtime concepts.

## Decision

- Runtime-owned public failures use runtime-neutral naming. `PycastleError` may remain in pycastle as a compatibility shim for existing callers, but it is not the generic public base error for `pycastle_agent_runtime`.
- `.pycastle-session` remains pycastle's compatibility session root, owned by the pycastle adapter/worktree layer. Runtime session APIs receive a supplied session root, provider session path, or path plan; the runtime package must not own `.pycastle-session` as a generic constant.
- Runtime-owned agent log records use neutral invocation vocabulary, such as `agent_invocation`. The existing `pycastle_input` record type may remain only as a compatibility schema for old pycastle-era logs or transitional pycastle adapters.
- Consuming projects continue to control pycastle log placement through `logs_dir`; non-pycastle runtime consumers pass an explicit effective log directory into runtime log APIs. Runtime log code owns reservation/appending mechanics, not pycastle config resolution.

## Consequences

- Runtime error/result contracts can be adopted by non-pycastle consumers without depending on pycastle-branded names.
- Pycastle keeps current operational paths and old log readability while the migration lands, but docs and tests must call retained pycastle names compatibility shims.
- Failure-report session paths should be supplied or formatted at the pycastle adapter/worktree boundary instead of being hardcoded by runtime errors.
- Package-boundary tests should reject runtime surfaces that reintroduce pycastle application imports or pycastle-owned layout defaults.

## Related

- Compatibility policy issue: https://github.com/Johannes-Kutsch/pycastle/issues/1658
- Boundary cleanup PRD: https://github.com/Johannes-Kutsch/pycastle/issues/1648
- Runtime ownership migration: ADR 0044
