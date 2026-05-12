# Pin implement-phase SHA from planner, not from cache re-derivation

`PlanReady` regains a `sha: str` field. Every entry to `implement_phase` carries the SHA at which the work was decided; `run_issue` consumes it directly and no longer calls `deps.preflight_cache.get_safe_sha(deps)`. `planning_phase` calls `get_safe_sha` once on both branches it returns through (normal plan, in-flight resume) and forwards `verdict.sha` on `PlanReady`. `_handle_preflight_outcome` forwards the `PreflightAFK.sha` it already holds into `_run_implement_and_merge`. `merge_phase` and `improve_phase` continue to call `get_safe_sha` themselves: improve owns the entry-point derivation, merge intentionally re-runs preflight on the post-merge tree.

The trigger was issue #679. ADR-0013 deleted `PlanReady.worktree_sha` on the explicit assumption that "the single-slot design plus internal lock means parallel `run_issue` callers see a populated slot and return without running preflight again." That assumption holds only while HEAD does not advance between the planner's `get_safe_sha` and the next consumer's. In #679 a doc commit pushed to the remote between iteration 3's planning and its implement phase; the second `get_safe_sha` saw a different HEAD, cache-missed, re-ran the same failing checks, filed duplicate issue #182, and pinned the implementer's worktree to the new SHA — a SHA the planner never approved and that included unrelated changes. The cache mechanic is fine; the design defect is that the planner's verdict is discarded the moment planning returns, forcing every downstream consumer to re-derive what was already known.

## Considered Options

- **Status quo: every `run_issue` re-derives via `get_safe_sha`.** Rejected: the failure mode in #679 — preflight pull fast-forwarding HEAD between phases, second cache call missing, duplicate issue filed, implementer pinned to an unvetted SHA — is intrinsic. Any push to the remote during an iteration triggers it.

- **Reuse the `preflight-sandbox` worktree as the implementer worktree on the preflight-fix path.** Rejected: the sandbox is a detached worktree owned by `get_safe_sha`'s lifetime; reusing it means promoting it to a branch-backed `managed_worktree` and extending its lifetime across phases. That is the same coupling ADR-0013 rejected under "Drive the preflight-fix recovery loop from inside `get_safe_sha()`". It also only fixes the preflight-fix path — the normal `PreflightReady` → planning → implement chain still suffers the same HEAD-advance race, just less visibly.

- **`get_safe_sha(deps, *, allow_advance=False)` — skip pull when caller declares it's inside a single iteration.** Rejected: pushes the cache invariant onto an optional flag that every implement-path caller must remember to set. Leaves "what does the SHA mean here" implicit at every call site rather than making it part of the data the planner produces.

- **Per-iteration cache scope.** Rejected: introduces a new concept (iteration boundary as cache state) to maintain alongside the SHA-keyed slot, just to paper over a missing data flow. Cheaper to thread the SHA the planner already has.

- **Thread the planner's SHA through `PlanReady` into `run_issue` (this ADR).** Chosen design.

## Consequences

- `PlanReady` gains `sha: str`. Tests that construct `PlanReady` without a SHA migrate.

- `planning_phase`'s in-flight branch now calls `get_safe_sha` before returning `PlanReady`; it can therefore return `PreflightAFK | PreflightHITL` like the normal branch. Behaviour change: when an in-flight branch is mid-work and `main` is broken, the iteration blocks on the freshly-filed preflight issue before resuming. This is a deliberate tightening — resuming in-flight work on a broken baseline is what produced the merge-skew incident that filed the original preflight issue in #679 in the first place.

- `_handle_preflight_outcome` forwards `result.sha` into `_run_implement_and_merge`. `_run_implement_and_merge` gains a `sha: str` parameter and forwards it into `implement_phase`. `implement_phase` forwards it into each `run_issue` call.

- `run_issue(issue, deps, ..., sha: str)` takes the SHA as a required positional-or-keyword argument and uses it directly for `managed_worktree(..., sha=sha)`. The `verdict = await deps.preflight_cache.get_safe_sha(deps)` call at the top of `run_issue` is deleted.

- `merge_phase` and `improve_phase` are unchanged. `merge_phase` deliberately re-runs preflight on a fresh tree the cache has never observed; `improve_phase` owns the entry-point derivation when no prior phase has set a SHA.

- The `PreflightCache` itself is unchanged. Its slot still survives across iterations; `improve_phase`, `planning_phase`, and `merge_phase` are the only remaining callers. The pull-on-every-call invariant in 0013 still holds for those three callers.

- Tests: `test_run_issue_implementer_worktree_uses_sha_from_preflight_cache` (test_implement.py:761) becomes `test_run_issue_pins_worktree_to_caller_supplied_sha`; assert `cache.get_safe_sha` is never called from `run_issue`. New regression test for #679: stub the cache to return `PreflightAFK(sha="X1", 181)` then `PreflightAFK(sha="X2", 182)` on successive calls, drive the iteration through `_handle_preflight_outcome`, assert exactly one `get_safe_sha` call total and the implementer worktree pinned to `X1`.
