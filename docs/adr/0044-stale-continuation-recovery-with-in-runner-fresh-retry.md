# Stale continuation recovery with in-runner Fresh retry

Before this ADR, `_invoke_runtime_attempts` in `agent_runner.py` caught only `RuntimeAgentCredentialFailureError` and `RuntimeHardAgentError`. When ar raised `RuntimeConfigurationError` (ar < 2.2) or `ContinuationUnrecoverableError` (ar ≥ 2.2) because a stale `_continuation` file could not be resolved into a live provider session, the error escaped to `implement_phase`, was collected as a run error, and pycastle returned `Continue()`. Since `_continuation` was never cleared, every subsequent iteration found the same resumable-session signal, attempted the same doomed resume, and failed identically — an infinite retry loop requiring manual operator intervention.

The same loop arose when the configured service changed (e.g. a fallback from Codex to OpenCode): `_prompt_run_state_for_role` correctly computed `RunKind.FRESH` based on the service mismatch, but the runner always re-read `role_session.run_kind()` directly and attempted `run_resumed_session`, hitting the same error.

## Decision

**Proactive service-mismatch detection.** At the start of every attempt loop iteration in `_invoke_runtime_attempts`, before calling `_run_runtime_once`, the runner compares `load_exact_transcript_service_name(role_session.path)` against `request.service`. If `is_resumable()` is True but the recorded service does not match the requested service, the runner takes the recovery path immediately without calling `run_resumed_session`.

**Reactive `ContinuationUnrecoverableError` catch.** `_invoke_runtime_attempts` catches `ContinuationUnrecoverableError` from ar (importable as `agent_runtime.errors.ContinuationUnrecoverableError`). Any other exception from `_run_runtime_once` propagates unchanged.

**Shared recovery path.** On either trigger:
1. Call `role_session.start_fresh()` to wipe the stale continuation and recreate the empty session dir.
2. Check `git_svc.is_working_tree_clean(request.mount_path)`. If dirty, update the `INTERRUPTED_WORK` key in `request.prompt.scope_args` with the clause text from `build_interrupted_work_clause(RunKind.FRESH, is_dirty=True)`.
3. Re-render the prompt via `_render_runtime_prompt` with `RunKind.FRESH`, replacing `current_prompt`.
4. Set `current_run_kind = RunKind.FRESH` and `continue` — retrying the attempt loop with a fresh session.

**`INTERRUPTED_WORK` only for dirty working trees.** Committed changes ahead of main are self-describing via `git log`; a fresh agent starting on an already-branched worktree will discover prior committed work through normal exploration. The `INTERRUPTED_WORK` clause fires only when the working tree itself has uncommitted changes that are not visible from git history alone.

## Considered options

- **Let `ContinuationUnrecoverableError` propagate.** The unhandled exception escapes to `implement_phase`, `_continuation` is never cleared, `is_resumable()` remains True next iteration, and the loop repeats indefinitely. Rejected.
- **Clear the continuation at the orchestrator/iteration boundary.** Would require the orchestrator to inspect role session state and mutate it — crossing the orchestration/session-lifecycle boundary. The runner already owns the retry loop and has access to `role_session`; recovery belongs there. Rejected.
- **Emit `INTERRUPTED_WORK` for committed changes ahead of main as well.** Committed changes are visible through `git log main..HEAD` and `git diff`; the agent's exploration step is expected to discover them. Adding a signal for committed changes would also require re-computing scope args after worktree creation (currently computed before), introducing architectural complexity for a case where the agent is not blind to prior work. Rejected.
- **Proactive-only (no `ContinuationUnrecoverableError` catch).** The proactive service-mismatch check covers intentional service switches. `ContinuationUnrecoverableError` also covers session expiry with the same service (Codex session token expired server-side while `_continuation` still exists locally). Both triggers need handling. Both kept.

## Consequences

- Stale continuations — from service changes, session expiry, or manual worktree recreation — self-heal within the current runner attempt loop without an orchestrator retry.
- The `INTERRUPTED_WORK` clause correctly fires for dirty working trees regardless of whether the Fresh restart was planned (service mismatch at plan time) or reactive (stale continuation caught at runtime).
- The runner requires access to `git_svc` inside `_invoke_runtime_attempts` to perform the dirty-tree check.
- `PromptInvocation.scope_args` must be mutable (or replaced) at recovery time to update `INTERRUPTED_WORK`; the runner constructs a new `PromptInvocation` with the updated scope_args dict rather than mutating the frozen dataclass.
