# Test obligation attaches to the artifact, not the slice mode

A **prose artifact** — any `.md` file outside `tests/` whose wording no caller observes, covering project documentation and shipped prompt templates — carries no test obligation in any slice mode. A `behavior-slice` may carry prose artifacts alongside code; they sit outside the behavior gate and outside the RED loop. Amends ADR 0013's behavior gate, which forbade `Edit`/`Write` on *any* non-test file before the first `<behavior>` tag.

Trigger: #2088 was a two-word edit to `coordination/plan.md` labelled `behavior-slice`. The gate left the implementer one reachable move — a failing test that string-matched the new prompt prose — so three such tests landed and were deleted by hand. The gate structurally forced the anti-pattern `CONTEXT.md` already documents as forbidden.

## Considered Options

- **Extend `docs-slice` to cover shipped prompt templates.** Rejected: "docs" would mean two different things, and prompt prose *is* product behavior. Leaves the mixed slice — code plus prose in one slice — untouched, and that is the general case.
- **Fourth slice mode for prose-only product text.** Rejected: same blind spot for mixed slices, at the cost of a fourth label and a fourth prompt.
- **Escape the gate when a slice's only artifacts are `.md`.** Rejected: narrowest blast radius, but it keys on the slice again, so a mixed slice still tests its prose.
- **Teach the classifier to route prose better.** Rejected as the primary fix: chasing labels while the obligation still hangs on the label. Tracked separately.

## Consequences

- **Prose artifacts are exempt in every slice mode**, `behavior-slice` included. The rule is stated once, in `shared/standards/_implementation.md`, which Implementer and Reviewer both render via `{{IMPLEMENTATION_STANDARDS}}`.
- **Behavior gate relaxed:** test files *and* the prose artifacts identified during Explore are writable throughout the session; every other file still waits for the first `<behavior>` tag carrying a real failing-test paste.
- **`work/behavior.md` splits acceptance criteria** into behaviors (run the RED loop) and prose artifacts (plain edits).
- **`improve/03-issues.md` gains a second criteria shape** for a `behavior-slice`'s prose parts — the file-state shape `docs-slice` uses — and stops listing `tests/` under *Files touched* for slices with no testable criteria.
- **The Reviewer can catch it now:** the `.md`-content anti-pattern reaches step 4's test-standards scan, which previously had no access to it — it lived only in `CONTEXT.md`, which the Implementer reads for vocabulary and the Reviewer not at all.
- **Three structural invariants over template prose stay valid:** placeholder resolution through the renderer, the placeholder-inventory sync test, and the no-unresolved-placeholder scan.
- **Classification is unchanged.** A prose-only slice can still be labelled `behavior-slice`. It now runs harmlessly through the gate exception, but nothing tells the agent the label was wrong; that gap is deliberate and tracked as follow-up.
- `work/refactor.md` and `work/docs.md` unchanged — neither carries a test obligation to relax.
