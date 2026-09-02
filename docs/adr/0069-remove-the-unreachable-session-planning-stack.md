# Remove the unreachable session-planning stack

Tracing the dead `record_successful_run` chain (ADR 0068) showed the chain was not the dead part — it was one branch of a subsystem that nothing reaches. The live `AgentRunner` path prepares its own session state inline and resumes from `_continuation` alone; the planning stack beside it computes run kinds, provider session ids and exact-transcript matches for callers that no longer exist.

The reachability argument, in full:

- `run_prompt` (`runtime.py`) is called only by `AgentRunner.run_prompt`, which has no caller anywhere in the repo.
- `run_resident_prompt`, `ResidentRunRequest`, `plan_resident_session` and `ResidentSessionPlan` have no callers at all, and had none before this change.
- `prepare_run_session` is reached only through `AgentRunner._prepare_session`, which is consumed only by `RuntimeInvocationDependencies.prepare_session` — that is, only by the three functions above plus `run_one_shot`, which ADR 0068 makes sessionless.
- `plan_provider_run_state` then has two callers: `plan_run_session` (on the path just eliminated) and `has_exact_transcript_match`, whose sole consumer was improve's phase-1→2 gate, re-based on `is_resumable()` by the ADR 0008 amendment.
- What the live path actually keeps of all this is one call: `service.provider_session_state(...)` in `_run_with_runtime_client`, whose entire result is discarded except `auth_seed_action`.

## Decision

**Delete the stack.** `run_prompt`, the resident path, `prepare_run_session` / `prepare_agent_run_session_state` / `AgentRunSessionState` / `PreparedAgentProviderRunSession`, `plan_run_session`, `plan_provider_run_state` / `ProviderRunStatePlan`, and `_preferred_provider_session_id` all go. `PromptRunRequest` stays — `run_one_shot` is aliased to it.

**Narrow the auth-seeding call.** `_run_with_runtime_client` asks `AgentService` for an auth-seed action directly instead of computing a whole `ProviderSessionState` to read one field. This is the call that made dead identity code look alive: it is the last live caller of the claude uuid derivation, and reading it is what made the namespace-truncation defect look like it was biting production when it was not.

**Land the replacement tests first, in their own change.** `tests/test_session_dispatch.py`, `tests/test_run_session.py` and `tests/test_agent_run_session_state.py` cover this stack in depth. Tests for ADR 0068's two predicates and for sessionless one-shot go green before any of it is removed, so the deletion never doubles as the removal of its own evidence.

## Considered options

- **Keep it as public API for future callers.** Rejected: it is precisely what made this bug hard to see. `AgentRunner.run_prompt` made the recording chain look reachable, and a reader tracing `record_successful_run` upward finds five plausible layers before discovering none of them run.
- **Delete only what ADR 0068 directly orphans, and file the rest as a follow-up.** Genuinely close. Rejected because the follow-up's whole content would be "prove these are unreachable, then remove them" — the same argument made here, made twice, with a release in between during which someone may wire a caller into it.
- **One commit for tests and deletion together.** Rejected: it leaves no revision at which the new predicates are covered and the old stack is not, so a reviewer cannot separate "was this unreachable?" from "is the replacement right?".
- **Fix the namespace truncation instead of deleting its reader.** See below.

## Consequences

- `_role_session_identity_from_path`, `session_uuid_for_role_session_path` and the per-service copies of the uuid derivation lose every consumer and go with the stack.
- **This collides with the decision recorded for #2238.** That issue records the session namespace as path-valued and repairs the single-segment reader behind one `RoleSession.from_path`. Under this ADR that reader has no callers left, so the truncation defect is dissolved rather than fixed. The two decisions are not compatible as written: #2238 keeps and canonicalises a reader this ADR deletes. Whichever lands second must reconcile them — either this ADR retains `from_path` as the namespace accessor for non-identity uses, or #2238's amendment is narrowed to the layout statement and drops the reader clause. **Unresolved at time of writing.**
- The claude provider session id derived from a role-session uuid ceases to exist. Nothing on the live path consumed it: it was computed in `_run_with_runtime_client` and discarded.
- `ProviderSessionState`, `ProviderSessionStateRequest` and `is_exact_resumable_service_session` lose their remaining consumers once the auth-seeding call is narrowed; `exact_transcript_match` stops being threaded through the plan types.
- The surviving live session surface is small and easy to state: `RoleSession` for lifecycle and `_continuation`, the ownership scan and `has_exact_transcript` from ADR 0068, and one auth-seed query on `AgentService`.
