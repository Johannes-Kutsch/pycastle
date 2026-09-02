# Transcript ownership derives from session-directory layout

`_service_session_metadata.json` recorded which service owned a role session's transcript and with which provider session id. Nothing on the live path ever wrote it. `ServiceSessionStore.record_successful_run` was reachable only through `pycastle.runtime.run_prompt` — which has no caller anywhere in the repo — and through `run_one_shot`, whose `_OneShotOutputAdapter.is_successful_result` returns `True` unconditionally, so every slice classification wrote a record naming the *plan* stage's service into the *Implementer's* role session in the plan sandbox. Not a missing write: a write with the wrong owner in the wrong directory.

Consequently `is_exact_resumable_service_session` returned False on its first clause in every production call, and three checks were inert:

1. improve's strict phase-1→2 gate (ADR 0008) — since re-based on `is_resumable()`;
2. ADR 0044's proactive service-mismatch detection, leaving only the reactive `ContinuationUnrecoverableError` catch;
3. `_prompt_run_state_for_role`'s exact-transcript handoff, which therefore emitted ADR 0044's `INTERRUPTED_WORK` clause on every dirty resumable run — including runs the runner then resumed from `_continuation`, where the clause is simply false.

## Decision

**Ownership is derived, never recorded.** `_service_session_metadata.json` is deleted, along with `record_successful_run` and the five layers above it. The provider state dir already *is* `<role_session>/<service>/` — `provider_state_relpath` uses `session_root=".pycastle-session"` and `SESSION_DIR_NAME` is `.pycastle-session` — so the filesystem already states which service ran in a role session. The JSON was a parallel record of a fact the layout carries.

**Two predicates, named for the two different questions.**

- `owning_service_name()` — the single service subdirectory under a role session that contains at least one file, intersected with the service names the registry knows. Returns `None` when zero or more than one qualify. This is what ADR 0044's runner check consults.
- `has_exact_transcript(...)` — `owning_service_name() == service.name` and `service.is_resumable(state_dir)`. This is what `_prompt_run_state_for_role` consults.

The scan is intersected with registry service names rather than taking any subdirectory, because a role session directory holds namespace subdirectories too: improve runs in `main` and `candidate/<N>`, so `improve/` holds `candidate/` beside `claude/`. No scanned path mixes them today; nothing in the layout prevents it.

**One-shot runs are sessionless.** `run_one_shot` no longer takes a role session, calls `start_fresh()`, or mounts provider state under `.pycastle-session/<role>/`. A one-shot has no resume semantics by construction — `_OneShotOutputAdapter` already hardcodes `RunKind.FRESH` and `provider_session_id=None`.

## Considered options

- **Wire `record_successful_run` into the `AgentRunner` path.** Rejected: it keeps a persisted record that only one site maintains and nothing validates, which is the mechanism that let this rot unnoticed. It also has nothing honest to record — the runner never captures a provider session id from the outcome, and on success it tears the session down anyway.
- **Derive ownership from the provider-session sidecar (`<service>/thread_id`) instead of the directory.** Rejected: the sidecar inherits the defect being fixed. It is written only when `select_resumable_provider_session_id` can recover an id from provider-owned files, so it is absent after a first fresh run — and for claude it has *no* live writer at all, because claude's `provider_session_state` derives an id from the role-session uuid and its `recover_provider_session_id` returns `None`. A predicate resting on it would be permanently false for the most-used service.
- **Keep the provider-specific exactness clause** (`is_exact_resumable_provider_session`). Rejected, and this is the real cost of this ADR. For codex it genuinely compares the recovered rollout thread id against the sidecar; dropping it means `has_exact_transcript` no longer distinguishes "*this* transcript is here" from "*a* transcript is here". It was rejected because it requires the sidecar, and the sidecar has no writer we would trust after this change — restoring it means first giving the sidecar a writer worth trusting, which is a separate decision. Two lesser reasons: for claude the clause is only `both arguments are non-None`, and for codex `recover_provider_session_id(None) == None` returns True, so the check passed on an absent state dir.
- **Write an explicit `_service` marker at run start** rather than deriving anything. Rejected: a hand-maintained record with one write site is exactly the shape that failed here.
- **Take any subdirectory as a service.** Rejected: correct only by accident of current namespace usage, and silently wrong the first time an implement session is namespaced.

## Consequences

- `ServiceSessionStore` collapses to path helpers plus the ownership scan. `save_service_session_id`, `get_service_session_id`, `select_resumable_provider_session_id`, `_SERVICE_SESSION_ID_FILENAMES` and the `ServiceResumeIdentityStore` protocol lose their live consumers.
- The metadata carve-out in `clear_provider_state_and_signal_completion` — which skipped `_service_session_metadata.json` while deleting everything else — is removed. Nothing needs ownership to survive teardown: both surviving consumers run only when the session was *not* cleared.
- ADR 0044's proactive service-mismatch detection fires for the first time in production. Its recovery path (`start_fresh` + fresh re-prompt) has until now run only from the reactive `ContinuationUnrecoverableError` catch.
- `_prompt_run_state_for_role` stops emitting `INTERRUPTED_WORK` on runs that actually resume. Its `run_kind` return has no reader in `src/` and is left alone here.
- The slice classifier stops wiping and recreating `<plan-sandbox>/.pycastle-session/implement/` on every classification. That directory also made `any_role_dir_present` true, which under ADR 0066 would have made the plan sandbox reusable on entry on the strength of a foreign agent's leftovers.
- Replacement tests for both predicates and for sessionless one-shot land and go green *before* the old stack is removed (ADR 0069), so the deletion is not also the removal of the only evidence the new behaviour works.
