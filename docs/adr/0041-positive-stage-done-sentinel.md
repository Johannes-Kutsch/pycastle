# Positive stage-done sentinel replaces negative-inference done predicate

> **Amends:** ADR 0005 (stage completion via role-session-dir state) and its ADR 0039 amendment.

After the ar migration (ADR 0039), `is_done()` relied on `path.is_dir() AND NOT is_resumable()` — inferring completion from the *absence* of a `_continuation` file. This introduced a false-positive: ar's `_codex_prepare_runtime_state` calls `provider_state_dir.mkdir(parents=True, exist_ok=True)` as part of session setup, and auth seeding (`LocalAuthSeedAction.apply()`) also creates the session directory before the provider runs. A startup credential failure (`AgentCredentialFailureError`) therefore leaves the session directory behind with no `_continuation` file, causing `is_done()` to return True for a session that never completed any work. On the next run the implement stage was skipped and the reviewer ran on an empty branch.

The invariant broken by ADR 0039 was: "dir appears only when agent first starts." After ar, the dir appears as a filesystem side-effect before the agent runs.

## Decision

`clear_provider_state_and_signal_completion()` writes a `_done` sentinel file inside `role_session.path` after clearing all provider state files. `is_done()` checks for the presence of `_done` only. Completion is now a positive signal — something explicitly written — rather than an inference from the absence of a resume signal.

`clear_provider_state_and_signal_completion()` is named to make the dual responsibility (purge state + write completion marker) explicit to readers.

## Considered options

- **`_done` sentinel file — chosen.** Single authoritative signal; immune to side-effects from auth seeding, AR session setup, or any future provider preparation step. Clean separation from `is_resumable()`, which stays `_continuation`-based.
- **Branch-commit scan.** Rejected: IMPLEMENTER and REVIEWER share the same branch; distinguishing which stage a commit belongs to requires fragile commit-counting. Reintroduces the coupling between "describe the work" and "mark stage done" that ADR 0005 explicitly rejected.
- **Restore old file-scan `is_resumable()`.** Rejected: the old scan found `auth.json` in the session tree and returned True, accidentally preventing the false-positive. This was coincidental protection, not an invariant. It would be broken again if auth seeding moves outside the session tree.
- **Clean up session dir on non-preservable failure.** Rejected: requires classifying every failure type at the runner boundary; misses the window where AR creates the dir before an exception can propagate to a cleanup handler.

## Consequences

- `is_done()` becomes `self._done_path().is_file()` — no dir check needed.
- `clear_provider_state_and_signal_completion()` replaces the old completion method; callers updated (`runner._invoke_runtime_attempts` and `implement.run_issue` for IMPLEMENTER and REVIEWER).
- `discard()` behaviour unchanged — it removes the entire session dir including any `_done` file. Ephemeral roles (Merger, Divergence-Resolver, Planner, Preflight-Issue, Improve) continue to call `discard()` rather than the renamed completion method.
- `start_fresh()` behaviour unchanged — `shutil.rmtree` already removes `_done` as part of the full directory deletion.
- Migration: sessions completed before this change have no `_done` file and will be re-run once. Re-running an already-complete Implementer or Reviewer is an acceptable one-time cost.
- The CONTEXT.md **stage-done signal** entry is updated to reflect the `_done`-file contract.
