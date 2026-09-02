# An interrupted improve cycle outranks planning, within a run

When a usage limit cuts an improve cycle off part-way through its candidate list, the next iteration in the **same `pycastle run`** re-enters improve and finishes drafting, even when the candidates already drafted have filed `ready-for-agent` tickets waiting. Planning waits until the candidate list is drained. Across runs there is no such priority: a restarted process finds no armed flag, planning wins, and the half-drafted tail is discarded.

## Why the two cases differ

The improve scan ranks candidates against one specific tree, which is why ADR 0050 makes `hash(safe_sha)` the improve fingerprint. Once a candidate's slices are implemented and merged, the remaining candidates were ranked against a tree that no longer exists, and re-scanning is the more correct answer, not merely the cheaper one. That is the cross-run case, and it stays as it was.

Inside one run and one sleep, nothing has merged. The tree is the same tree the scan ranked against, the candidate list on disk is still valid, and the only reason planning would win is an ordering accident: `_run_iteration_inner` reads the issue list before it reaches the idle gate, and the tickets improve itself filed one iteration earlier now make that gate false. Yielding there throws away drafting work for candidates that are still perfectly good.

## Decision

`deps.improve_cycle_interrupted` widens from "don't stop at sleep" to **"improve owns the next iteration"**. It is already process-scoped: armed on `UsageLimitError` at `iteration/__init__.py:348`, carried across iterations by the orchestrator (`orchestrator.py:354-386`), cleared after any normal `improve_phase` return, and gone when the process ends — which is exactly the same-run-only scope this rule needs, with no new persistence.

The idle gate becomes `(not open_issues and not in_flight) or deps.improve_cycle_interrupted`.

**The fingerprint gate still outranks the flag.** If the operating branch moved during the sleep, `_gate_and_wind_down` discards the improve session as before. Improve still owns the iteration and still runs before planning; it runs a fresh scan instead of resuming. Drafting *priority* and drafting *resumption* are separate guarantees and only the second is lost — which keeps this consistent with treating a moved SHA as invalidating.

**Only `UsageLimitError` arms it.** An agent timeout leaves an identically half-finished list, but arrives as an `AbortedTimeout` outcome rather than an exception through that gate, so wiring it is a second change; deferred until it shows up in practice.

## Considered options

- **A second flag** (`improve_drafting_incomplete`) armed only when candidates were pending, leaving `improve_cycle_interrupted` alone. Rejected: both flags would be armed at the same site, cleared at the same site, and threaded through the same `Deps` and orchestrator carry-over, differing only in whether the scan had already produced candidates — and an interruption *during* the scan is still drafting that should resume.
- **Resume across a moved SHA inside one run.** Rejected: the Spec Agent would write specs against a tree that no longer exists, the precise failure ADR 0050's fingerprint exists to prevent.
- **Drain first unconditionally** — improve finishes its whole candidate list before its tickets become eligible at all, in every run. Rejected: it inverts the idle-fill model. Improve exists to fill idle time; making it hold up work that is ready is the opposite of that.
- **Persist the candidate list across SHA changes and re-validate each remaining candidate.** Rejected: carries candidates whose ranking is stale, and per-candidate re-validation starts to look like a second scan.

## Consequences

- Within a run, tickets filed by an earlier candidate can sit `ready-for-agent` across several iterations while improve finishes drafting. That is the point, but it is visible to an operator watching the board and is worth saying out loud.
- The gate's `in_flight` half cannot disagree with its `open_issues` half here: improve only starts when in-flight is empty, and nothing during an improve cycle creates in-flight work.
- Clearing stays unconditional after a normal `improve_phase` return. A cap-reached stop yields `Done(improve_cap_reached=True)` next iteration anyway, and a SHA-change stop leaves candidates that are stale by definition.
- Cross-run behaviour is unchanged: a restarted process re-scans, and `_wind_down_partial_candidates` handles any partly-filed candidate. A candidate interrupted mid-spec has no `_candidate_record` — `spec_number` is only set by `file_draft_set` — so it is dropped silently with nothing to close.
- Depends on ADR 0066 for the resumption half: without sandbox reuse on enter, the flag would return improve to a wiped sandbox and a fresh scan every time.
