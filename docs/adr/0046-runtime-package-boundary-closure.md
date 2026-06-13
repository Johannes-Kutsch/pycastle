# Runtime package boundary closure

The review of the `pycastle_agent_runtime` extraction against issue #857 found that the runtime package has moved well beyond the original facade, but several seams still keep the reusable boundary from being complete under ADR 0044 and ADR 0045.

Issue #857's target remains an ownership migration, not just a local package name. The runtime package should be usable as a narrow standalone runtime for already-rendered prompts, while pycastle remains the adapter for issue orchestration, prompt rendering, output protocol parsing, CLI wiring, and pycastle compatibility paths.

## Decision

- `pycastle_agent_runtime` must not ship pycastle application orchestration. Pycastle issue orchestration belongs in pycastle, even when exposed through a local adapter for CLI tests. A runtime package module that imports `pycastle.agents`, `pycastle.iteration`, `pycastle.services`, `pycastle.session`, or pycastle display code is not part of the reusable package boundary.
- Runtime-owned text-output execution includes reducing runtime parsed provider events into a text result or runtime error. The generic rule for `Result`, `AssistantTurn`, `PromptTokens`, `UsageLimit`, `TransientError`, `HardError`, and `CredentialFailure` belongs in `pycastle_agent_runtime`; pycastle's container runner may keep Docker I/O and status presentation but should call the runtime-owned reducer.
- Runtime public errors must not default to pycastle or Claude vocabulary. Pycastle compatibility values such as `.pycastle-session`, `PycastleError`, and legacy service names are supplied by the pycastle adapter boundary. Generic runtime failures either require explicit caller-supplied paths/service names or use neutral absence, and `pycastle_agent_runtime` must not expose `PycastleError` as a public runtime base error.
- Runtime session planning owns the provider run-state workflow, but concrete provider policy belongs behind a public, narrow provider session adapter contract. Runtime planning should not hardcode facts such as Codex host auth files, Codex rollout parsing, OpenCode service-state exceptions, OpenCode sidecar filenames, or Claude's preferred session id. Provider implementations supply those facts while preserving pycastle's existing `.pycastle-session` state compatibility.
- Runtime work invocation may fill missing credential-failure service identity from the selected service, but provider adapters must emit correct service identity. Generic runtime code should not correct provider identity through provider-name special cases.
- The legacy pycastle provider identity API is transitional compatibility, not a second supported decision model. `RoleSession` may remain as pycastle's `.pycastle-session` storage adapter, but `ProviderIdentity`, `ProviderIdentityKind`, `ExactTranscriptHandoff`, and `RoleSession.provider_identity()` should be retired once runtime session planning covers their callers and tests.
- Package-boundary tests must cover every shipped runtime submodule, not only top-level exports and selected lazy attributes. If a module is distributed as part of `pycastle_agent_runtime`, it must be importable without pycastle application modules unless it is moved out of the runtime package. The same proof must cover built artifacts, not only the editable source tree.

## Consequences

- Boundary completion now includes removing or relocating any shipped runtime module that exists only to delegate to pycastle application orchestration.
- Package contract tests should prove standalone importability for all distributed runtime modules, runtime-owned text-output behavior, provider-session adapter contracts, and built package artifacts without importing pycastle.
- Pycastle adapter tests should prove current pycastle behavior: prompt rendering, protocol-output parsing, `.pycastle-session` compatibility paths, status display formatting, setup failure translation, provider credential routing, and CLI orchestration.
- Provider-specific session details remain supported, but through explicit provider session adapters rather than string checks in generic session planning or work invocation.
- Retiring the legacy pycastle provider identity API is part of closing the #857 migration, not optional unrelated cleanup.

## Related

- Original extraction concept: https://github.com/Johannes-Kutsch/pycastle/issues/857
- Runtime ownership migration: ADR 0044
- Runtime compatibility artifact policy: ADR 0045
