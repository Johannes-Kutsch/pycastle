# Unified status-row wrapper with kind / discipline / colour as kwargs

`iteration/_rows.py` previously exposed two async context managers — `phase_row` and `agent_row` — that diverged on three axes: (1) the `kind` arg passed to `register`, (2) whether `UsageLimitError` / `AgentTimeoutError` rendered as `"interrupted"` or `"failed"`, (3) whether a verdict-declaration discipline was enforced ("clean exit without `.close()` paints `"failed"`"). Agent colour was driven by an implicit regex on the caller name string (`#N` → palette index `N % 9`). Preflight's request to declare `"finished, all tests green"` on the green path forced a decision between bolting a verdict-declaration mechanism onto `agent_row` and accepting two parallel wrappers that diverged only by ceremony.

Collapse both wrappers into a single `status_row` async context manager parameterised on three orthogonal kwargs: `kind` (`"phase" | "agent"` — drives **blank-line rule** + style metadata only), `must_close: bool` (the verdict-declaration discipline — `True` for phases, `False` for agents), and `color_key: int | None` (palette index source — pass `issue_number` for Implement/Review rows, leave `None` everywhere else). Apply the previous `phase_row` exception handling uniformly: `UsageLimitError` and `AgentTimeoutError` paint `"interrupted"` regardless of `kind`.

## Behaviour changes

- **Agent rows now paint `"interrupted"` on `UsageLimitError` / `AgentTimeoutError`**, where they previously painted `"failed"`. The phase row above still paints `"interrupted"` at the centralised iteration boundary (ADR 0006) — both painters are now truthful, and an agent that crashed because of a usage limit is not classified as malfunctioning.
- **Palette index for agent colour is taken from the `color_key` kwarg, not parsed from the caller name string.** `Implement Agent #N` and `Review Agent #N` continue to share colour because both call sites pass `color_key=N`. The cyan-bold rendering of the literal `#N` substring inside the `[Caller]` prefix is a separate cosmetic rule on the caller string and is retained — it never drove row identity.

## Considered options

- **Keep `phase_row` and `agent_row` as separate wrappers; add `.close()` to `agent_row` for Preflight's verdict.** Rejected: the two wrappers were 80% the same, and the remaining differences mapped cleanly to kwargs. Keeping the split bought nothing beyond named-factory grep-ability, and would have left "agent declares a verdict via mutable attribute / phase declares a verdict via `.close()`" as two patterns for the same concept.
- **Keep the wrappers split, share an internal `Row` primitive between them.** Rejected as premature: shared primitives ossify before the second variable-verdict use case appears. The kwarg split is reversible; a shared private class plus two thin wrappers would have committed to a shape with `n = 1`.
- **Leave palette index parsed from caller name; only add `colored: bool` toggle.** Rejected: keeps the implicit name-regex that the rest of this change explicitises away. A reader who sees `kind`, `must_close`, and `colored` as kwargs would reasonably expect the colour input to also be a kwarg, not a hidden parse rule on the caller string.
- **Keep agent rows' `"failed"` paint on `UsageLimitError`.** Rejected: agent rows that abort because of a usage limit are not malfunctioning. Painting `"interrupted"` matches the truth of the situation, matches the phase row's verdict for the same exception class, and removes one of the three reasons the wrappers had to diverge.

## Consequences

- `iteration/_rows.py` exports one async CM. `phase_row` and `agent_row` glossary entries collapse into one `status_row` entry. CONTEXT.md `agent color` entry rewrites to reference `color_key` rather than caller-name parsing.
- All current `phase_row(...)` call sites become `status_row(kind="phase", must_close=True, initial_phase=...)`; current `agent_row(...)` call sites become `status_row(kind="agent", color_key=N or None, work_body=...)`. The Preflight Agent call site additionally calls `row.close("finished, all tests green")` iff the failures list returned by `runner.preflight(...)` is empty.
- The palette-lookup code (currently in `RichStatusDisplay`) drops its caller-name `#N` regex and reads `color_key` from the registration call.
- `AgentRunner._run` (Implement / Review path) pulls the issue number from `request.scope_args` and forwards it as `color_key`.
- Test suite consolidation: `tests/test_rows.py` collapses `test_phase_row_*` and `test_agent_row_*` cases onto the unified API; the `ctx is None` assertion in the agent success-path test becomes an assertion on the yielded `StatusRow` handle's defaults.
- Agents that hit a usage limit mid-run will produce `[Agent] usage limit reached` in the status output instead of `[Agent] failed` — visible UI change with no behavioural follow-on (the phase row's `AbortedUsageLimit` outcome is unchanged).
