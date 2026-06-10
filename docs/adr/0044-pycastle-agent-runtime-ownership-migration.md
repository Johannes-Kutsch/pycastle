# Pycastle agent runtime ownership migration

Issue #857 introduced `pycastle_agent_runtime` as a local package, but the first extraction left it as a facade over pycastle application internals. The package owns useful pieces such as service registry resolution, stage priority-chain helpers, prompt-oriented runtime entrypoints, and the agent log lifecycle, but it still imports pycastle-owned contracts and calls private pycastle runner members for the core Work lifecycle.

We will complete the migration by making `pycastle_agent_runtime` the owner of the reusable agent-runtime contract and making pycastle consume that package through an adapter. This is an ownership migration, not only a code move.

The runtime package owns shared runtime vocabulary and behavior: `StageOverride`, the `AgentService` protocol, parsed provider events, `RunKind`, provider session state request/result types, service registry resolution, stage priority-chain selection, Work invocation lifecycle, provider run-state/session metadata, sleep/wake availability decisions, text-output execution, runtime-facing error/result contracts, and agent log lifecycle.

Pycastle keeps pycastle-specific semantics above that seam: prompt-family rendering, agent output protocol parsing, issue readiness, planning issue intake, preflight issue filing, failure-report prompt content, merge orchestration, CLI commands, config loading, and pycastle-specific status presentation.

The generic package entrypoint accepts an already-rendered prompt, a nested `StageOverride` chain, tool policy, worktree mount, optional session namespace, and optional run-session plan. It resolves the service chain, manages the reusable Work lifecycle, and returns the LLM text result. Pycastle's `AgentRunner` becomes a role adapter that renders pycastle prompts, supplies the pycastle protocol-output adapter, calls the runtime package, and translates pycastle-specific output/failure semantics.

The package must not depend on pycastle application modules such as `pycastle.agents`, `pycastle.session`, `pycastle.infrastructure`, `pycastle.iteration`, or `pycastle.prompts` once the migration is complete. If container execution remains physically implemented in pycastle during an intermediate slice, the runtime package receives it through an injected execution adapter rather than importing pycastle internals.

The first stable surface remains narrow and shared. We are not designing a general SDK for unknown consumers. The target consumers are pycastle first and application-pipeline later.

Compatibility names that still carry pycastle vocabulary follow ADR 0045: `PycastleError`, `.pycastle-session`, and `pycastle_input` may be retained only as compatibility shims. Runtime-owned public errors, session-path handling, and agent log record vocabulary use runtime-neutral names and caller-supplied paths.

## Consequences

- Import ownership becomes a release criterion: `pycastle_agent_runtime` must be importable and testable without importing pycastle application modules.
- Package contract tests move with shared behavior: service selection, fallback, resume, sleep/wake decisions, Work invocation, provider session metadata, text-output execution, and agent log behavior.
- Pycastle integration tests focus on adapter behavior: prompt rendering, role mapping, protocol-output parsing, issue orchestration, and pycastle failure translation.
- Compatibility re-exports from pycastle may remain temporarily, but they are transition shims rather than the final public boundary.
- Old pycastle runtime modules are retired only after parity is proven through package contract tests and pycastle adapter tests.
- Packaging must keep a clear path to a standalone `pycastle_agent_runtime` distribution, even if publishing happens after the local migration.
- The migration preserves current stage priority-chain semantics from ADR 0034 and the credential-failure routing policy from ADR 0043.

## Related

- Follow-up PRD: https://github.com/Johannes-Kutsch/pycastle/issues/1631
- Original closed migration epic: https://github.com/Johannes-Kutsch/pycastle/issues/857
