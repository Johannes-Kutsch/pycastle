# `max_parallel` is a concurrency cap, not a per-iteration batch cap

`max_parallel` limits how many agents run concurrently. It does **not** cap the number of issues processed per iteration. Every issue the planner returns runs in the iteration it was planned in; the agent semaphore queues the rest, and one merge phase closes the iteration.

Implementation: `implement_phase` owns two coordinating semaphores.

- **Agent semaphore** — `asyncio.Semaphore(max_parallel)`, acquired inside `_bounded_run_agent` around the actual `agent_runner.run` call. Gates how many Implementers/Reviewers execute at once.
- **Worktree semaphore** — `asyncio.Semaphore(max_parallel + 1)`, acquired around every `managed_worktree(...)` block in `run_issue` (both the Implementer's and the Reviewer's). Caps the number of live worktrees at `max_parallel + 1`: enough for every running agent plus one pre-staged so the next agent can start without waiting on `git worktree add`.

The status-row counter tracks implement and review starts separately: `"Running: started implement Agents for X/Y issues · started review Agents for Z/Y issues"`. `Y` is the full planner output, not `max_parallel`. `X` increments on each agent-semaphore acquire by an Implementer; `Z` increments on each acquire by a Reviewer. The review segment appears only after the first Reviewer starts.

The trigger was issue #874: with `max_parallel=5` and 7 planned issues, the implement-row read `5/5` and only 5 issues were implemented. Root cause: `run_iteration` truncated the planned list with `issues = issues[: deps.cfg.max_parallel]` before dispatching to `implement_phase`, so both the denominator (`len(issues)`) and the executed set were the truncated 5. The slice was redundant — `implement_phase` already had an agent semaphore — and was load-bearing only for an undocumented "one merge per issue in sequential mode" side effect.

## Considered Options

- **Keep the slice; fix only the counter.** Rejected: the user's report explicitly stated that only 5 of 7 issues were implemented and they expected all 7. Fixing the display without changing behaviour leaves the substantive bug in place.
- **Drop the slice; keep only the agent semaphore.** Rejected: with the planner returning N issues and only `max_parallel` running, the other N − max_parallel `run_issue` calls would still eagerly open worktrees up front. For large plans this fans out disk usage and `git worktree` operations unbounded.
- **Drop the slice; cap the per-iteration batch with a new config knob (e.g. `max_per_iteration`).** Rejected: introduces a second tuning dial whose default no operator would know how to choose. The planner already bounds the set to `ready-for-agent` minus blocked; in practice it stays small. Adds knob without removing one.
- **Drop the slice; gate `managed_worktree` with a semaphore at exactly `max_parallel`.** Rejected: makes the next Implementer wait for the previous worktree to be torn down before its own `git worktree add` runs, leaving the agent semaphore underutilised between issues. The `+1` slot is the prefetch that keeps the agent semaphore saturated.
- **Drop the slice; two semaphores — agent at `max_parallel`, worktrees at `max_parallel + 1` — chosen.** Bounds disk fan-out at a small constant above the concurrency target, keeps agents saturated, and matches the user's invariant: "at most `max_parallel + 1` worktrees on disk at any moment, at most `max_parallel` agents running."

## Consequences

- `iteration/__init__.py` no longer slices `plan_result.issues`. The full list flows into `implement_phase`.
- `implement_phase` constructs two semaphores. The agent semaphore is the existing one. The worktree semaphore is new and is threaded into `run_issue` (alongside the agent semaphore), where it wraps each `managed_worktree` block.
- The Implementer worktree closes before the Reviewer worktree opens within a single issue, so the worktree semaphore per-issue usage is at most one slot at a time. Cross-issue, slot reuse staggers naturally.
- `"Running: started implement Agents for X/Y issues · started review Agents for Z/Y issues"`: `Y = len(plan_result.issues)`, `X` increments on each Implementer semaphore acquire, `Z` on each Reviewer acquire. The review segment appears only once the first Reviewer starts. Both counters terminate at `Y/Y`.
- `max_parallel = 1` still serialises agents (one Implementer or Reviewer at a time, with one extra worktree pre-staged) but **all** planned issues run within the same iteration and a **single** merge phase closes that iteration. The previous "per-issue merge with safe-SHA re-pinning" side effect is gone — branches in the same iteration share their base SHA. Conflict elimination for `max_parallel = 1` is therefore no longer a property of the system; users who need it should run smaller plans or rely on the merge-phase preflight + divergence-resolver.
- The `sequential mode` term and its conflict-elimination claim are removed from `CONTEXT.md`; the `max_parallel` glossary entry is added in its place.
- The `iteration` glossary entry no longer mentions a "sequential mode: each issue gets its own merge before next" branch — every iteration ends in exactly one merge phase.
