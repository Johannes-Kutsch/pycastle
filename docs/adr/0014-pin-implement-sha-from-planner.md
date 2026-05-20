# Pin implement-phase SHA from planner, not from cache re-derivation

`PlanReady` regains `sha: str`. Every entry to `implement_phase` carries the SHA at which the work was decided; `run_issue` consumes it directly and no longer calls `deps.preflight_cache.get_safe_sha(deps)`. `planning_phase` calls `get_safe_sha` once on both branches (normal plan, in-flight resume) and forwards `verdict.sha` on `PlanReady`. `_handle_preflight_outcome` forwards the `PreflightAFK.sha` it already holds into `_run_implement_and_merge`. `merge_phase` and `improve_phase` continue to call `get_safe_sha` themselves: improve owns entry-point derivation, merge intentionally re-runs preflight on the post-merge tree.

Trigger was #679: ADR 0013 assumed parallel `run_issue` callers would see the cache's populated slot — only true while HEAD doesn't advance between planner's `get_safe_sha` and the next consumer's. A doc commit pushed to remote between iteration 3's planning and implement caused a cache-miss, re-ran failing checks, filed duplicate issue #182, and pinned the implementer to a SHA the planner never approved. The cache mechanic is fine; the defect is discarding the planner's verdict the moment planning returns.

## Considered Options

- **Status quo: every `run_issue` re-derives via `get_safe_sha`.** Rejected: HEAD-advance race is intrinsic.
- **Reuse `preflight-sandbox` as implementer worktree on preflight-fix path.** Rejected: same coupling ADR 0013 rejected under "drive recovery from inside `get_safe_sha`"; only fixes preflight-fix path.
- **`get_safe_sha(deps, *, allow_advance=False)` flag.** Rejected: pushes the cache invariant onto an optional flag every caller must remember.
- **Per-iteration cache scope.** Rejected: introduces a new concept to paper over a missing data flow.
- **Thread the planner's SHA through `PlanReady` into `run_issue` — chosen.**

## Consequences

- `PlanReady` gains `sha: str`. Tests constructing `PlanReady` without SHA migrate.
- `planning_phase`'s in-flight branch calls `get_safe_sha` before returning `PlanReady`; can now return `PreflightAFK | PreflightHITL` like normal branch. **Behaviour change:** in-flight branch + broken `main` blocks on the freshly-filed preflight issue before resuming. Deliberate tightening — resuming in-flight on a broken baseline produced #679 in the first place.
- `_handle_preflight_outcome` forwards `result.sha` into `_run_implement_and_merge`; `implement_phase` forwards into each `run_issue` call.
- `run_issue(issue, deps, ..., sha: str)` takes SHA as required arg; uses it for `managed_worktree(..., sha=sha)`. The `get_safe_sha` call at the top of `run_issue` is deleted.
- `merge_phase` and `improve_phase` unchanged.
- `PreflightCache` unchanged; pull-on-every-call invariant from ADR 0013 still holds for `improve_phase`, `planning_phase`, `merge_phase`.
- Regression test: stub cache to return `PreflightAFK(sha="X1", 181)` then `PreflightAFK(sha="X2", 182)`; assert exactly one `get_safe_sha` call total and implementer worktree pinned to `X1`.
