# Remove skip_preflight, route Merger through PreflightCache, delete PreflightFailure

`RunRequest.skip_preflight`, the `runner.preflight()` call inside `agent_runner.run()`, and `PreflightFailure` are deleted. The Merger — previously the only agent that did not pass `skip_preflight=True` — now asks the cache directly: `merge_phase` calls `await deps.preflight_cache.get_safe_sha(deps)` over the post-clean-merge HEAD and pattern-matches the verdict. `PreflightReady` proceeds to construct the merge-sandbox at `verdict.sha` and spawn the Merger. `PreflightAFK | PreflightHITL` logs the merge-time diagnostic and returns a soft-skip `MergeResult` — identical behaviour to the `except PreflightFailure` block it replaces.

This is slice 3 of #639, completing the design introduced in ADR 0013.

## Considered Options

- **Keep `runner.preflight()` inside `agent_runner.run()` with `skip_preflight` defaulting to `True`.** Rejected: cosmetic. Six callers would drop `skip_preflight=True`; the Merger would write `skip_preflight=False`; the asymmetry "every caller except one disables this" would remain. The plumbing is the same, spelled differently. Removing the in-runner preflight entirely is cleaner — the Merger's check is not "an agent prelude" but a separate question about a post-merge tree whose SHA is now part of the cache's universe.

- **Remove `runner.preflight()` and the Merger's check entirely.** Rejected: loses a real safety net. After clean-merges, HEAD's content is novel — it has never passed `PREFLIGHT_CHECKS` because no previous step held a tree with that exact combination of branch contents. Without the merge-time check, the Merger may succeed on a broken baseline and fast-forward broken code to `main` before the next iteration's gate notices.

- **Route through `get_safe_sha()` as chosen.** The merge-time check benefits from the cache (same post-clean-merge SHA queried twice in an iteration yields one preflight run), shares the `PreflightReady | PreflightAFK | PreflightHITL` failure shape with improve and planning, and the soft-skip semantics are preserved at the call site in `merge_phase` rather than buried inside agent_runner.

## Consequences

- `RunRequest.skip_preflight` removed; all six previous call sites that passed `skip_preflight=True` updated (field no longer exists).
- `runner.preflight()` call removed from `AgentRunner._run()`.
- `PreflightFailure` deleted from `errors.py`; no remaining import or raise site.
- `merge_phase` calls `get_safe_sha()` over the post-clean-merge HEAD; the `try/except PreflightFailure` block is replaced by a pattern-match on the verdict.
- The merge-sandbox is constructed at `verdict.sha` (from the cache) rather than from `git_svc.get_head_sha()` directly.
- ADR 0013 marked superseded with a pointer to this ADR.
