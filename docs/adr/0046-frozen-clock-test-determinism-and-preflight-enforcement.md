# Frozen-clock test determinism, preflight-enforced; CI watch loop deferred

Recurring post-merge CI failures across pycastle and its consuming projects (application-pipeline, agent_runtime) were analyzed from ~140 workflow runs. The dominant genuine failure class was **time-bomb tests**: hardcoded absolute datetimes compared against a real clock read inside the code under test — green at author time and through every existing gate (preflight, implement checks, review, host checks all run *before* the date passes), then red days later on an unrelated push. Remaining genuine classes: non-hermetic filesystem-state tests, environment-gap regressions, and sleep-based race tests. About a third of red runs were not test failures at all (ci-autofix's by-design abort, PyPI/network flakes).

## Decision

**Prevention-first; detection deferred.** The fix is authored at the test-writing source, not as a post-failure repair loop. A CI watch/fix agent is explicitly deferred until residual failure data (after prevention lands) shows it is warranted — its hard problem is classification, since a naive "workflow failed → spawn fixer" would fire on autofix machinery and network flakes, which must be retried or ignored, never "fixed".

**Suite-wide frozen clock, pinned to a fixed *past* instant.** `tests/conftest.py` gains an autouse `time_machine.travel(..., tick=True)` fixture. A fixed instant makes any hardcoded-date-vs-real-clock test fail *immediately at author time* — inside the implementing agent's own feedback loop — instead of detonating later. The instant is in the **past** because file mtimes come from the real filesystem clock: files written during a test then read as future (conservatively fresh), so retention/staleness sweeps in the code under test never delete files a test just wrote. Age-sensitive tests state age explicitly (`os.utime` relative to the frozen `time.time()`). `tick=True` keeps timeout/deadline loops progressing.

**Enforcement is mechanical, via a `frozen-clock` entry in the default `PREFLIGHT_CHECKS`.** The check fails when `tests/conftest.py` exists but lacks an autouse `time_machine` fixture, and passes trivially when there is no `tests/conftest.py`. In an unmigrated consuming project the existing preflight-failure path takes over: the preflight-issue agent files one AFK issue and the normal pipeline lands the fixture — once per project, with zero recurring prompt cost. Non-Python projects override `preflight_checks` as usual (field-by-field config override).

**Prose standard + reviewer scan carry the classes a mechanical gate cannot.** The implementation-standards fragment gains a "Deterministic & Portable Tests" section (verdict must be a pure function of the code under test — never wall clock, timezone, host OS, filesystem state, environment, ordering, or network), and the review prompt's test-standards step explicitly scans changed tests for time bombs, pre-existing-state dependence, OS assumptions, ordering, and sleep-based synchronization. Canonical authoring lives in the maintainer's local skills (`tdd/tests.md`) and is ported into the bundled prompt standards, keeping skills as the source of truth.

## Considered options

- **Post-run CI watch/fix agent as primary.** Rejected for now: reactive (full CI latency + fix cycle per miss), whack-a-mole for dormant time bombs, and ~1/3 of observed red runs are non-fixable noise requiring a classification layer before any remediation. Revisit with residual failure data.
- **Ruff `DTZ` rules / static lint for datetime literals.** Rejected: the observed bombs were tz-*aware* (`DTZ` misses them), and the suite legitimately contains ~143 absolute-datetime literals as *injected* `now=` inputs — the good pattern is statically indistinguishable from the bomb. Only controlling the clock at runtime discriminates.
- **Forbid real clock reads (raise on `datetime.now()`/`time.time()`), forcing injection everywhere.** Strongest design pressure, but requires routing the existing scattered direct clock reads through a seam first. Rejected as upfront cost; the prose standard nudges toward injection incrementally.
- **Future-instant freeze.** Rejected: real mtimes then read as years-stale and retention sweeps delete files tests just wrote (observed: log-maintenance tests failed under a 2030 freeze).
- **Ship the fixture via `pycastle init` scaffolding.** Rejected: conflicts with the no-init-scaffolding stance (ADR 0030) and every project's conftest differs.
- **Embed the fixture snippet in the implement prompt.** Rejected: recurring prompt-token cost in every agent call for a once-per-project setup problem.

## Consequences

- `time-machine` becomes a dev dependency of pycastle and, over time, of consuming projects (added when their fixture-installation issue is fixed).
- Each unmigrated consuming project will fail the new preflight check once and receive one auto-filed AFK issue; until that issue is merged, AFK work on that project is gated (same semantics as any failing preflight check).
- Tests that relied on real "now" agreeing with real file mtimes must pin mtimes explicitly — this is the intended discipline, not incidental breakage.
- The `config.py.example` preflight block now contains a multi-line shell one-liner; the init test verifies it by `ast`-parsing the rendered example and comparing against the live `Config` default, keeping scaffold text and runtime defaults provably in sync.
- Residual CI failures after this lands become the dataset that decides whether a detection loop is built at all.
