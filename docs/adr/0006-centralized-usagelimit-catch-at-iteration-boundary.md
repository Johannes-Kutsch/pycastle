# Centralized `UsageLimitError` catch at the iteration boundary

`run_iteration` wraps its body in a single `try/except UsageLimitError → return AbortedUsageLimit(reset_time=err.reset_time)`. Per-phase try/excepts inside Preflight/Plan/Improve/Merge are deleted. `phase_row` learns `shutdown_style="interrupted"` and its `finally` paints `"interrupted"` on `UsageLimitError`, `"failed"` otherwise. `implement_phase` keeps its bespoke `usage_limit_hit` / `usage_limit_reset_time` aggregation on `ImplementResult` because it runs N parallel agents and must commit completed siblings before unwinding.

Trigger was issue #538: the Improve agent hit usage-limit, `UsageLimitError` propagated out of `improve_phase`, orchestrator crashed instead of standard sleep/failover. `git log` shows two prior fixes for the identical class (`2d55eaf`, `8e94c3b`) — recurring foot-gun.

## Considered Options

- **Per-phase try/except (status quo).** Rejected: handlers byte-identical; forgetting one is a silent crash. Three recurrences.
- **Structural sum type per phase.** Rejected: makes "forgot to handle" a type error, but preserves boilerplate and threads `reset_time` through every return contract.
- **Convert at `agent_runner.run`.** Rejected: invasive; every caller acquires a new variant; dispatcher still needs per-phase handling at a different layer.
- **Lint/AST rule.** Rejected: tests structure not behaviour; brittle.
- **Top-level try/except in orchestrator loop, removing `AbortedUsageLimit`.** Rejected: breaks `IterationOutcome` symmetry — three outcomes are values and one becomes a side-channel.
- **`phase_row` paints `"failed"` on every exception including `UsageLimitError`.** Rejected: misreads as phase malfunction directly above the orchestrator's "sleeping until …" message.
- **Single `run_iteration` try/except + `phase_row` `"interrupted"` style + keep `implement_phase` aggregation — chosen.**

## Consequences

- Per-phase `try/except UsageLimitError` blocks at `iteration/__init__.py:71-75` (Preflight) and `:116-120` (Plan) deleted. Improve / Merge arms gain no wrapping — covered by top-level catch.
- `phase_row.shutdown_style` accepts `"interrupted"`; `finally` at `_rows.py:33-35` paints `"interrupted"` on `UsageLimitError`, `"error"` otherwise; re-raises either way.
- `implement_phase`'s `ImplementResult.usage_limit_hit` / `usage_limit_reset_time` stay; dispatcher pattern-matches on the result struct.
- Future phases calling `agent_runner.run` don't need boilerplate — top-level catch covers them.
- Parametrized regression test in `tests/test_iteration.py` injects `UsageLimitError` at each single-agent phase entry and asserts `AbortedUsageLimit`.
