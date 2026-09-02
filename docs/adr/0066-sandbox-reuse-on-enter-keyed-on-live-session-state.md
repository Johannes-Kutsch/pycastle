# Sandbox reuse on enter is keyed on live session state

A reusable sandbox worktree — improve-sandbox, plan-sandbox, diverge-sandbox — is reused on entry when the expected branch is checked out and a role session dir is present, and rebuilt otherwise. The **fingerprint gate** (ADR 0050) is the sole staleness authority for these sandboxes: on a moved safe SHA it discards the role session, which makes the worktree non-reusable and forces the rebuild. This restores the **worktree reuse on enter** rule that `CONTEXT.md` has always described, and which the reusable-sandbox lifecycle had silently lost.

Trigger was issue #2223: the Improve agent hit a usage limit during phase 02, slept, and the next iteration re-ran the phase 01 scan from scratch. Everything needed to resume was on disk and intact — `_continuation` is written *before* `UsageLimited` is turned into `UsageLimitError` (`runner.py:786-788`), and `_teardown_worktree_branch` deliberately keeps the worktree because `any_role_dir_present` is true. The state survived the sleep and was destroyed on the *next entry*, because `managed_worktree` routed every non-durable-issue lifecycle through `_cleanup_stale_named_worktree` + `_create_worktree`.

## How the two rules collided

`d1f01377` ("Fix #896 — ephemeral sandbox worktrees now always rebuilt at requested SHA") made the blanket rebuild deliberate: a sandbox left behind by an earlier run must not hand the agent a stale base. `#1706` then refactored the lifecycle flags into `BranchWorktreeLifecycle` and gave one predicate, `_deletes_branch_on_teardown`, two jobs — at teardown "may I delete this branch?", at entry "must I demolish and rebuild?". ADR 0050 later added the context fingerprint, which answers `#896`'s freshness worry precisely and per role, and its text assumes the entry path already reuses (*"the session dir is gone, `_cleanup_stale_named_worktree` can then remove the worktree"*). Nobody went back to relax the blanket rule, so ADR 0050's conditional ephemeral resume was unreachable for every role it named.

## Decision

**Two predicates, each named for what it asserts.**

- `_branch_is_disposable(lifecycle)` — true except `DURABLE_ISSUE`. Consulted at both ends, because "this branch is pycastle's to throw away" is genuinely the same fact at entry and at teardown. An issue branch is never disposable.
- `_rebuilds_on_enter(lifecycle)` — true only for `REPLACEABLE_MERGE_SANDBOX`. Forces a rebuild even when session state is present, keeping ADR 0026's guarantee that stale conflict state cannot resume from an obsolete baseline.

Entry rebuilds when `_rebuilds_on_enter(lifecycle) or not is_worktree_reusable(path, branch, git_svc)`. The rebuild arm is unchanged: `_cleanup_stale_named_worktree` when the branch is disposable, then `_create_worktree`. `#896`'s guarantee therefore survives intact for every case except the one deliberately added.

**Reuse is cause-agnostic.** Entry mirrors exit. The exit side already asks "does this worktree still carry session state?" (**broadened preservation rule**); entry now asks the same question, whatever ended the previous run — usage limit, transient error, credential failure, timeout, Ctrl-C, container crash. Keying entry on the exception class instead would re-create the two-predicates-kept-in-sync-by-hand shape that caused this defect.

**`.preserved-failure` is demoted to an exit-side signal.** It no longer guards entry. A failure that leaves session state behind is already protected by the reuse rule; a failure that leaves none has nothing left to preserve but a bare checkout. Leaving it as an entry guard was actively harmful: the marker has no delete site anywhere in the codebase, and `RoleSession.discard()` removes `.pycastle-session/<role>/`, not `.pycastle-session/.preserved-failure` — so after one hard failure the sandbox was pinned at its old checkout forever, and a fingerprint mismatch would discard the session without the rebuild it was supposed to trigger. The entry path clears the marker when it rebuilds.

## Considered options

- **Move improve's state out of the worktree** (`_candidate_list`, `_candidate_cursor`, `_in_flight`, `candidates/`, `_drafts/`) into a durable per-project location, leaving the rebuild alone. Rejected: it cannot recover the provider transcript, which is the expensive part of what an interrupted Spec Agent loses, and it fixes nothing for Planner or Divergence-Resolver, which lose the same work today for the same reason.
- **Fix it for improve only.** Rejected: the defect is in the shared lifecycle. Special-casing one `SandboxWorktreeIntent` inside an intent-agnostic predicate is the same conflation one enum member later.
- **Add `UsageLimitError` to the set that writes a preservation marker.** Rejected: it keeps two predicates that must be hand-synchronised, and it overloads `.preserved-failure` to mean both "keep for human diagnosis" and "resume works here".
- **Let reusable sandboxes fall through to the plain `_create_worktree` arm** that `DURABLE_ISSUE` uses, relying on `_rebase_reused_branch_onto` to hard-reset. Rejected: that helper returns early when `_carries_own_work` is true, silently handing the agent a stale base. Sandbox branches are never committed to by design, and "by design" is exactly the assumption that fails quietly.
- **Rename nothing; add only `_rebuilds_on_enter`.** Rejected: it leaves a predicate named for a teardown effect at an entry-path call site, which is how this defect was born.
- **Per-lifecycle policy dataclass.** Rejected: a bigger refactor than three enum members justify, and it hides ADR 0026's exception in a table rather than naming it.

## Consequences

- Interrupted Planner, Divergence-Resolver and Improve sessions resume on the next iteration, as ADR 0050 always specified. Merger inherits the fix through its fingerprint-match branch, which already routes to `reusable_sandbox_worktree`; its `replaceable_merge_sandbox_worktree` path keeps rebuilding unconditionally.
- The Merger's fingerprint-match resume now rests on role-dir presence rather than on a `.preserved-failure` marker. `tests/test_merge.py:2685` leans on the marker only because its git mock never stubs `get_current_branch`; production has a matching branch and a `_continuation` in the role dir.
- `tests/test_orchestrator.py test_usage_limit_in_improve_resumes_then_stops` asserted the Scan Agent is dispatched twice — the bug, written down as expected behaviour under a name promising the opposite. Rewritten to assert resumption.
- Roughly twenty-five `tests/test_improve.py` tests pre-seed the sandbox session dir and stub the branch to match, so they flip from the rebuild arm to the reuse arm. Their fixtures gain project files so a reused worktree resembles one `_create_worktree` would have accepted.
- The operator sees a resume notice naming candidate and phase, mirroring the existing "Restarting improve from phase 1 because …" line. This bug ran for whole iterations while looking like normal operation.
- A separate defect found while tracing and filed on its own: `_service_session_metadata.json` is never written on the `AgentRunner` path, so improve's strict phase-1→2 gate, ADR 0044's proactive service-mismatch detection, and cross-service transcript-ownership checks are all inert. This ADR does not depend on it — see the ADR 0008 amendment.
