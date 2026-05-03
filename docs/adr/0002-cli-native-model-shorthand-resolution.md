# ADR 0002: CLI-native model shorthand resolution over load-time API call

**Status:** Accepted  
**Date:** 2026-05-03

## Context

`Config.plan_override.model` (and the equivalent fields for implement, review, and merge stages) accepts either a full Claude model ID (`claude-sonnet-4-6`) or a shorthand (`sonnet`). Before this decision, `load_config` resolved shorthands to full model IDs at config load time by calling `claude_service.list_models()` — a subprocess call to the Claude CLI — and selecting the latest matching model.

Two approaches were considered:

**Option A — Resolve at load time (previous behaviour):**  
`load_config` instantiates `ClaudeService`, calls `list_models()`, and replaces any shorthand in the config with the resolved full model ID before returning. The returned `Config` always contains fully-resolved model IDs.

**Option B — Pass through to the CLI:**  
`load_config` is pure (file I/O only). The model string is passed as-is to the Claude CLI at stage execution time. The CLI resolves shorthands natively.

## Decision

**Option B.** Model shorthand resolution is delegated to the Claude CLI at stage execution time.

## Reasons

- **Hidden interface cost.** Option A makes `load_config` appear to be a pure file-loading operation but introduces a subprocess call as a hidden side effect. Callers — including tests — must know to mock `ClaudeService` to avoid hitting the CLI.
- **Verified CLI support.** The Claude CLI accepts shorthands directly (`claude --model sonnet` works). There is no need to pre-resolve them.
- **Locality of validation.** Invalid model strings surface as CLI errors at the point of use, where the context (which stage, which run) is most relevant.
- **Testability.** A pure `load_config` can be tested with plain `Config` comparisons and no mocks.

## Consequences

- `Config.plan_override.model` (and equivalent fields) may hold a shorthand or a full model ID — callers cannot distinguish between them by type alone.
- Invalid model strings are not caught at startup. A bad model value surfaces as a CLI error when the relevant stage first runs, not when config is loaded.
- `validator.py` and its `_fetch_models` / `_resolve_shorthand` machinery are removed. Effort validation (a pure set-membership check) moves inline into `load_config`.
- `load_config` no longer accepts or instantiates a `claude_service` argument.
