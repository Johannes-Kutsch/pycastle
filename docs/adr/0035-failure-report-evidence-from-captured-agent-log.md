# Failure-Report evidence from the captured agent-invocation log

The **Failure-Report agent** is promised one canonical piece of diagnostic evidence — **Failure-Report evidence**, a copy of the failed run's **agent-invocation log** (header plus raw provider stream, all attempts) placed inside the mounted worktree for it to read with its own tools. pycastle no longer points diagnosis at the provider's native session, and `SESSION_DIR` keeps its single meaning as the provider-state / resume-cleanup pointer.

This is a boundary decision: a provider transcript belongs to the service that produced it (`provider transcript ownership`) and cannot be replayed across an agent/environment boundary. The trigger was an OpenCode Planner `protocol_error` whose provider-state dir held only a `session_id` file; the Failure-Report agent ran `opencode export <id>` from a different environment, got `Session not found`, and filed a content-free report. The service-aware Failure-Report `SESSION_DIR` decision had already foreshadowed this in its rejected alternatives ("Pass the whole agent log file path instead of `SESSION_DIR` … useful as a future improvement"); this ADR realizes that improvement while preserving that `SESSION_DIR` service-state path.

## Considered Options

- **Provider-native session export as the contract** (status quo). Rejected: requires the provider CLI, the right provider-home env, and a non-expired session inside a *different* agent's environment — structurally unreliable and provider-specific.
- **Persist a provider export at failure time.** Rejected: a weaker special-case of provider export; still depends on the provider being able to export at failure, which the incident disproves.
- **Inline-inject the captured record into the prompt.** Rejected: the provider JSON event stream is far bulkier than a diff, and the codebase already commits to "do not inline bulky content; let the agent inspect via tool calls" (`interrupted-work prompt clause`). Truncating to fit risks cutting the failure tail.
- **Copy the captured log into the worktree (chosen).** Provider-agnostic, survives session expiry, no provider-CLI dependency, full fidelity preserved on disk; the agent already has Read/Bash in that worktree.

## Consequences

- The contract is generic across every service; no OpenCode special-case and no `opencode export` dependency, so `Session not found` can no longer occur in diagnosis.
- The failed run's captured-log path is carried to the failure boundary alongside the failed service name (mirrors the `service_name` threading through `AgentFailedError` / `translate_run_outcome`).
- The copy lands in the worktree's session area already excluded from git, so it never shows as a project change.
- The promise is bounded to what pycastle captured: a usable log is copied (a near-empty one still evidences that the agent produced nothing); when none exists, the existing **Failure-Report fallback issue** path is unchanged.
- `SESSION_DIR` is untouched — still the provider-state directory and the `non_typed_crash` `rm -rf <SESSION_DIR>` recovery target.
- #1826 (continuation prompt sent to a service that does not own the transcript on a mid-run service change) is a separate fix sharing only the `provider transcript ownership` concept.
