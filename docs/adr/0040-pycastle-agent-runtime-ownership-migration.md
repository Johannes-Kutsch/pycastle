# Pycastle agent runtime ownership migration

`pycastle_agent_runtime` started as a facade over pycastle internals. The migration makes it the owner of the reusable agent-runtime contract, with pycastle consuming it through an adapter.

**Runtime package owns:** `StageOverride`, `AgentService` protocol, parsed provider events, `RunKind`, provider session state types, service registry resolution, stage priority-chain selection, Work invocation lifecycle, provider run-state/session metadata, sleep/wake decisions, text-output execution, runtime error/result contracts, agent log lifecycle, and generic text-output event reduction (`Result`, `AssistantTurn`, `PromptTokens`, `UsageLimit`, `TransientError`, `HardError`, `CredentialFailure`).

**Pycastle keeps:** prompt-family rendering, agent output protocol parsing, issue readiness, planning issue intake, preflight issue filing, failure-report prompt content, merge orchestration, CLI commands, config loading, status presentation, `.pycastle-session` compatibility paths.

The generic entrypoint accepts an already-rendered prompt, nested `StageOverride` chain, tool policy, worktree mount, optional session namespace, and optional run-session plan. Two public surfaces: stage-aware one-shot call path and resumable resident-agent path.

## Compatibility artifact policy

Runtime-owned surfaces use runtime-neutral naming. Pycastle-vocabulary artifacts may remain only as compatibility shims:

- `PycastleError` — compatibility shim in pycastle, not the generic runtime base error.
- `.pycastle-session` — pycastle's compatibility session root. Runtime APIs receive caller-supplied roots/paths.
- `pycastle_input` — compatibility log record schema. Runtime uses `agent_invocation`.
- `ProviderIdentity`, `ProviderIdentityKind`, `ExactTranscriptHandoff`, `RoleSession.provider_identity()` — transitional; retire once runtime session planning covers their callers.

## Boundary rules

- `pycastle_agent_runtime` must not import `pycastle.agents`, `pycastle.iteration`, `pycastle.services`, `pycastle.session`, `pycastle.infrastructure`, `pycastle.prompts`, or pycastle display code.
- Runtime session planning owns provider run-state workflow; concrete provider policy belongs behind provider session adapter contracts, not hardcoded in generic planning.
- Runtime work invocation must not correct provider identity through provider-name special cases.
- Package-boundary tests must cover every shipped runtime submodule and built artifacts for standalone importability.

## Consequences

- Import ownership is a release criterion.
- Package contract tests cover: service selection, fallback, resume, sleep/wake, Work invocation, provider session metadata, text-output execution, agent log behavior, standalone importability.
- Pycastle adapter tests cover: prompt rendering, protocol-output parsing, `.pycastle-session` compatibility, status display, setup failure translation, credential routing, CLI orchestration.
- Preserves stage priority-chain semantics from ADR 0031 and credential-failure routing from ADR 0039.

## Related

- Follow-up PRD: https://github.com/Johannes-Kutsch/pycastle/issues/1631
- Original extraction: https://github.com/Johannes-Kutsch/pycastle/issues/857
- Compatibility policy: https://github.com/Johannes-Kutsch/pycastle/issues/1658
- Boundary cleanup PRD: https://github.com/Johannes-Kutsch/pycastle/issues/1648
