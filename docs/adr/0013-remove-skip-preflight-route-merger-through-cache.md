# Content-addressed PreflightCache; remove skip_preflight, route Merger through cache

The iteration-scoped preflight gate (`ensure_preflight` + `Deps.preflight_verdict`) collapses into a single `PreflightCache` owned by the orchestrator and threaded through every iteration's `Deps`. The cache holds **one slot**: the most recently observed safe SHA's verdict (`PreflightReady | PreflightAFK | PreflightHITL`). Every save-SHA-consuming caller invokes `await deps.preflight_cache.get_safe_sha(deps)`; the method serialises via `asyncio.Lock`, runs `git pull --ff-only`, returns the cached verdict iff the slot's SHA matches HEAD, otherwise runs preflight in an ephemeral `transient_worktree("preflight-sandbox")` and updates the slot. The cache is constructed once in `orchestrator.run()` outside the iteration loop.

`RunRequest.skip_preflight`, the `runner.preflight()` call inside `agent_runner.run()`, and `PreflightFailure` are deleted. The Merger — previously the only agent that did not pass `skip_preflight=True` — calls `await deps.preflight_cache.get_safe_sha(deps)` directly inside `merge_phase` over the post-clean-merge HEAD and pattern-matches the verdict: `PreflightReady` proceeds to construct the merge-sandbox at `verdict.sha`; `PreflightAFK | PreflightHITL` soft-skips, returning `MergeResult(clean=clean_issues, conflicts=conflict_issues)` — identical behaviour to the former `except PreflightFailure` block.

The trigger was issue #639: iteration 2 re-ran preflight over the same improve-sandbox at the same HEAD SHA after a usage-limit account switch. Two redundancies: `Deps.preflight_verdict` is reconstructed every iteration discarding valid cached verdicts; and the predicate "is this SHA safe" is structurally content-addressable — the answer depends only on the tree's content and the configured check list. A SHA-keyed single-slot cache survives across iteration boundaries without weakening any existing safety property.

## Considered Options

- **Status quo: iteration-scoped `Deps.preflight_verdict`.** Rejected: the cache loses information at iteration boundaries that is structurally still valid — every usage-limit retry, every iteration that doesn't advance HEAD, repeats work whose result has not changed.

- **Lift the cache to orchestrator scope but keep iteration-keyed lookup.** Rejected: iterations are an orchestration concept, not a content concept. Two iterations at the same HEAD with the same checks have the same answer; keying on iteration loses information in both directions.

- **SHA-keyed multi-slot cache (`set[str]` of confirmed-safe SHAs, or `dict` holding both safe and broken SHAs).** Rejected: `git pull --ff-only` only moves HEAD forward; once past a SHA it is never queried again. Single-slot is semantically equivalent for every observable behaviour, with smaller surface and no eviction concept.

- **Persist the cache across `pycastle` restarts.** Rejected: cache content depends on `cfg.preflight_checks`, every check-list edit, and every behaviour change in check tooling. A stale persistent cache could silently certify a now-broken SHA. Process-scoped is honest.

- **Drive the preflight-fix recovery loop from inside `get_safe_sha()`.** Rejected: structurally wrong. The orchestrator's iteration loop already implements this at iteration granularity. Folding it into `get_safe_sha()` introduces recursion (the implement phase is itself a `get_safe_sha()` consumer), demands a coarse-grained lock blocking every concurrent caller across the whole recovery run, and conflates "ask for a SHA" with "drive a recovery sub-orchestration."

- **Keep `runner.preflight()` inside `agent_runner.run()` but invert `skip_preflight` to default `True`.** Rejected: cosmetic. Six callers stop writing `skip_preflight=True`, the Merger writes `skip_preflight=False`, and the asymmetry "every caller except one disables this" stays. The cleaner step is removing the in-runner preflight entirely — the Merger's check is not "an agent prelude" but a separate question about a post-merge tree whose SHA is now part of the cache's universe.

- **Remove `runner.preflight()` and the Merger's check entirely.** Rejected: after clean-merges, HEAD's content is novel — no previous step held a tree with that exact combination of branch contents. Without the merge-time check, the Merger may succeed on a broken baseline and fast-forward broken code to `main` before the next iteration's gate notices. The right move is routing through `get_safe_sha()`.

- **Single-slot SHA-keyed cache, lifted to orchestrator scope, called via `get_safe_sha()`, with `skip_preflight` and `runner.preflight()` removed; `PreflightHITL.worktree_sha` renamed `sha`.** The chosen design.

## Consequences

- A new class `PreflightCache` is introduced (in `iteration/preflight.py`) with one private slot `_verdict: PreflightResult | None`, one `asyncio.Lock`, and `async def get_safe_sha(self, deps) -> PreflightResult`. `orchestrator.run()` constructs `PreflightCache()` once before the iteration loop and threads the same instance into every `IterationDeps`.

- `Deps.preflight_verdict: PreflightReady | None` is removed. `Deps.preflight_cache: PreflightCache` is added.

- `ensure_preflight(deps, mount_path)` is removed. Its two callers — `improve_phase` and `planning_phase` — call `deps.preflight_cache.get_safe_sha(deps)` directly. The `mount_path` argument disappears; the cache opens its own `transient_worktree("preflight-sandbox", sha=sha)`. Consumer phases still create their own sandbox afterwards via `checkout_detached` at `verdict.sha`.

- `PlanReady.worktree_sha` is removed. `implement_phase` calls `deps.preflight_cache.get_safe_sha(deps)` itself at entry. The single-slot design plus internal lock means parallel `run_issue` callers see a populated slot and return without running preflight again.

- `RunRequest.skip_preflight` removed; all six previous call sites that passed `skip_preflight=True` updated. `runner.preflight()` call removed from `AgentRunner._run()`.

- `PreflightFailure` deleted from `errors.py`; no remaining import or raise site. The merge-time `try/except PreflightFailure` block in `merge.py` is replaced by the verdict pattern-match.

- `merge_phase` calls `get_safe_sha()` over the post-clean-merge HEAD. `PreflightReady` → merge-sandbox at `verdict.sha`; `PreflightAFK | PreflightHITL` → soft-skip returning `MergeResult` with conflicts open.

- All three verdict types (`PreflightReady`, `PreflightAFK`, `PreflightHITL`) now expose a uniform `.sha` field (`PreflightHITL.worktree_sha` renamed `sha`). Call sites that read `result.worktree_sha` switch to `result.sha`.

- Pull-on-every-call means `git_svc.pull()` runs inside every `get_safe_sha()` invocation, gated by the cache's lock. In normal flow (upstream not advancing) this is a no-op pull plus a cache hit. The lock serialises parallel `run_issue` callers so only one pays the pull cost.

- Tests that injected `Deps.preflight_verdict` migrate to seeding `PreflightCache._verdict` or supplying a `PreflightCache` subclass whose `get_safe_sha()` returns a fixture verdict. Tests for the merge-time preflight path switch to asserting on `get_safe_sha()` in `merge_phase` and the absence of any `runner.preflight()` invocation.
