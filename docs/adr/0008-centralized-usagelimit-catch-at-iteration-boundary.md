# Centralized `UsageLimitError` catch at the iteration boundary

`run_iteration` wraps its body in a single `try/except UsageLimitError → return AbortedUsageLimit(reset_time=err.reset_time)` and the per-phase try/excepts inside the Preflight, Plan, Improve, and Merge dispatcher arms are deleted. `phase_row` learns a third `shutdown_style="interrupted"` and its `finally` clause distinguishes `UsageLimitError` (paint `"interrupted"` and re-raise) from other exceptions (paint `"failed"` and re-raise). `implement_phase` keeps its bespoke `usage_limit_hit: bool` / `usage_limit_reset_time: datetime | None` aggregation on `ImplementResult` because it runs N parallel agents and must commit completed siblings before unwinding.

The trigger was issue #538: the Improve agent hit the usage limit, `UsageLimitError` propagated out of `improve_phase`, and the orchestrator crashed instead of taking the standard sleep / account-failover path. `git log` shows two prior fixes for the identical class — `2d55eaf` ("catch UsageLimitError in plan phase") and `8e94c3b` ("fix unhandled UsageLimitError in post-merge preflight") — confirming this as a recurring foot-gun rather than a one-off.

## Considered Options

- **Status quo (per-phase try/except).** Rejected: the handlers are byte-identical, there is no per-phase variation in behaviour, and forgetting to add one is a silent crash. Three recurrences in tracked history.
- **Structural sum type per phase (`PhaseResult | UsageLimitHit`).** Rejected: makes "forgot to handle" a type error rather than a runtime error, but preserves per-phase boilerplate and forces every phase to thread `reset_time` through its return contract.
- **Convert at `agent_runner.run` (callers pattern-match a `UsageLimitHit` sentinel).** Rejected: invasive. Every caller of `agent_runner.run` acquires a new variant; the dispatcher still needs per-phase handling at a different layer.
- **Lint/AST rule that fails CI when a call site lacks usage-limit handling.** Rejected: tests structure rather than behaviour; brittle to refactors; doesn't actually prevent the bug.
- **Top-level try/except in the orchestrator's iteration loop, removing `AbortedUsageLimit` from `IterationOutcome`.** Rejected: `IterationOutcome` is the contract between iteration and orchestrator; pushing one outcome out as an exception breaks symmetry — three outcomes are values and one becomes a control-flow side-channel.
- **`phase_row` paints `"failed"` on every exception including `UsageLimitError`.** Rejected: a row painted `"failed"` directly above the orchestrator's `"sleeping until 16:00"` message reads as a phase malfunction when the phase actually didn't malfunction.
- **Single try/except in `run_iteration` + `phase_row` `"interrupted"` style + keep `implement_phase` aggregation.** The chosen design.

## Consequences

- The per-phase `try/except UsageLimitError` blocks at `iteration/__init__.py:71-75` (Preflight) and `iteration/__init__.py:116-120` (Plan) are deleted. The `DispatchImprove` arm and `await merge_phase(...)` gain no per-arm wrapping — covered by the top-level catch.
- `run_iteration`'s body is wrapped: `try: ... except UsageLimitError as err: return AbortedUsageLimit(reset_time=err.reset_time)`.
- `phase_row.shutdown_style` accepts `"interrupted"`. The `finally` at `_rows.py:33-35` paints `"interrupted"` on `UsageLimitError`, `"error"` otherwise; re-raises either way.
- `implement_phase`'s `ImplementResult.usage_limit_hit` and `usage_limit_reset_time` fields stay. The dispatcher arm continues to pattern-match on the result struct.
- Future maintainers adding a phase that calls `agent_runner.run` no longer need to remember `try/except UsageLimitError` — the top-level catch covers them.
- A parametrized regression test in `tests/test_iteration.py` injects `UsageLimitError` at each single-agent phase entry point and asserts `run_iteration` returns `AbortedUsageLimit(reset_time=...)`. Adding a new single-agent phase becomes a one-line parameter addition.
