# Three-mode implement slice with explicit gate

Split implement work into three mutually-exclusive slice modes (`refactor-slice`, `behavior-slice`, `docs-slice`) marked by GitHub labels, each dispatched to its own prompt. **Slicing rule:** any step not verifiable by a new test of observable behavior goes into a `refactor-slice`. Multiple refactor steps may combine; dependent behavior slice lists it in `Blocked by`.

Trigger: under context pressure the agent skips TDD structure. Three causes: (a) `to-issues` writes criteria as tests; (b) issues mix refactor with behavior; (c) no checkpoint forces re-entry into TDD.

## Considered Options

- **Fix only issues or only prompt.** Rejected: both have structural problems.
- **One prompt with conditional sections.** Rejected: keeps bloat that caused skipping.
- **Silent default label (absence implies behavior).** Rejected: drift risk.
- **`AgentRole` as dispatch axis.** Rejected: slice mode is per-issue, not per-stage.
- **No gate; trust TDD framing.** Rejected: status quo, demonstrably insufficient.

## Consequences

- **Three labels mandatory per code-or-docs issue.** Exactly one of `refactor-slice`, `behavior-slice`, `docs-slice`. Canonical label set grows to nine.
- **Three prompts:** `work/behavior.md` (TDD with gate), `work/refactor.md` (no new tests), `work/docs.md` (markdown only). Prompt paths updated by ADR 0037.
- **Behavior gate:** explore → name behavior → write failing test → emit `<behavior>` tag → implement to green. No `Edit`/`Write` on non-test files before tag emits.
- **Refactor gate:** no new tests; uncovered behavior filed as follow-up `behavior-slice`.
- **Docs gate:** no code, no tests.
- **Dispatch:** `pick_implement_template(issue, cfg)` reads labels, fails fast on missing/unknown/multiple.
- No worktree/orchestration changes — slice mode is per-issue.
