# Content-addressed PreflightCache; remove skip_preflight, route Merger through cache

The iteration-scoped preflight gate (`ensure_preflight` + `Deps.preflight_verdict`) collapses into a single `PreflightCache` owned by the orchestrator and threaded through every iteration's `Deps`. **One slot:** the most recently observed safe SHA's verdict (`PreflightReady | PreflightAFK | PreflightHITL`). Every save-SHA-consuming caller invokes `await deps.preflight_cache.get_safe_sha(deps)`; the method serialises via `asyncio.Lock`, runs `git pull --ff-only`, returns the cached verdict iff slot SHA matches HEAD, otherwise runs preflight in an ephemeral `transient_worktree("preflight-sandbox")` and updates the slot. Constructed once in `orchestrator.run()` outside the iteration loop.

`RunRequest.skip_preflight`, `runner.preflight()` inside `agent_runner.run()`, and `PreflightFailure` deleted. The Merger — previously the only agent not passing `skip_preflight=True` — calls `get_safe_sha(deps)` directly inside `merge_phase` over post-clean-merge HEAD and pattern-matches: `PreflightReady` → construct merge-sandbox at `verdict.sha`; `PreflightAFK | PreflightHITL` → soft-skip returning `MergeResult(clean, conflicts)`.

Trigger was #639: iteration 2 re-ran preflight over the same improve-sandbox at the same HEAD after a usage-limit account switch. Two redundancies: `Deps.preflight_verdict` is reconstructed every iteration; "is this SHA safe" is structurally content-addressable.

## Considered Options

- **Iteration-scoped `Deps.preflight_verdict` (status quo).** Rejected: loses structurally-valid information at iteration boundaries.
- **Orchestrator-scoped cache, iteration-keyed lookup.** Rejected: iterations are an orchestration concept, not content.
- **SHA-keyed multi-slot cache.** Rejected: `git pull --ff-only` only moves HEAD forward; single-slot is semantically equivalent with smaller surface.
- **Persist cache across `pycastle` restarts.** Rejected: cache depends on `cfg.preflight_checks` + check tooling behaviour; stale persistence could certify a now-broken SHA.
- **Drive preflight-fix recovery from inside `get_safe_sha()`.** Rejected: orchestrator already implements this at iteration granularity. Folding it in introduces recursion, demands a coarse lock blocking concurrent callers, and conflates "ask for SHA" with "drive sub-orchestration."
- **Keep `runner.preflight()`, invert `skip_preflight` to default True.** Rejected: cosmetic; asymmetry remains.
- **Remove `runner.preflight()` and Merger's check entirely.** Rejected: after clean-merges HEAD's content is novel; without the check Merger may succeed on a broken baseline and fast-forward broken code.
- **Single-slot SHA-keyed cache via `get_safe_sha()` + remove `skip_preflight` + rename `PreflightHITL.worktree_sha` → `sha` — chosen.**

## Consequences

> ADR 0014 amends: `implement_phase` consumes `PlanReady.sha` from the planner instead of calling `get_safe_sha` itself; remaining cache callers are `improve_phase`, `planning_phase`, `merge_phase`.

- New `PreflightCache` class in `iteration/preflight.py`: private `_verdict: PreflightResult | None`, `asyncio.Lock`, `async def get_safe_sha(self, deps) -> PreflightResult`. `orchestrator.run()` constructs once and threads into every `IterationDeps`.
- `Deps.preflight_verdict` removed; `Deps.preflight_cache: PreflightCache` added.
- `ensure_preflight(deps, mount_path)` removed; callers call `deps.preflight_cache.get_safe_sha(deps)` directly. Cache opens its own `transient_worktree("preflight-sandbox", sha=sha)`. Consumer phases create their own sandbox via `checkout_detached` at `verdict.sha`.
- `PlanReady.worktree_sha` removed.
- `RunRequest.skip_preflight` removed; six previous call sites updated. `runner.preflight()` call removed from `AgentRunner._run()`.
- `PreflightFailure` deleted from `errors.py`. Merge-time `try/except PreflightFailure` block replaced by verdict pattern-match.
- All three verdict types expose uniform `.sha` (`PreflightHITL.worktree_sha` → `sha`).
- `git_svc.pull()` runs inside every `get_safe_sha()`, gated by the lock. Normal flow: no-op pull + cache hit.
- Tests injecting `Deps.preflight_verdict` migrate to seeding `PreflightCache._verdict` or a subclass returning a fixture verdict.
