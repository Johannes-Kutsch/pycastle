# Two-gate blocking

Status: accepted. The mechanical gate lands with #2111; the judgement gate already runs.

Blocking has been recorded three ways and enforced none. `file_draft_set` writes a `Blocked by #N` body line *and* registers a native GitHub dependency edge (ADR 0058). No pycastle code reads either: `add_issue_dependency` is write-only, and `_normalize_open_issue_item` discards `issue_dependencies_summary` before the Planner ever sees an issue. What actually decides whether work starts is the plan prompt, which asks the model to infer dependencies from issue prose — "B requires code or infrastructure that A introduces".

Two documents described mechanisms that were not running. ADR 0058 said the body line existed for "the Planner's plain-text scan"; there is no such scan. `docs/agents/issue-tracker.md` said the native edge was "the live gate"; nothing queried it. Issues #2106 and #2107 are what that costs: their bodies list blockers, no native edge was ever created for them, and they read as startable.

**Blocking is enforced by two gates in series, and both are load-bearing.**

**Gate 1 — mechanical, before the Planner.** A pure decision function drops issues whose `issue_dependencies_summary.blocked_by` is greater than zero from the ready-for-agent candidate set. GitHub returns the field on the issues list endpoint, so the gate costs no extra request. It is exact, cheap, and cannot be reasoned around.

**Gate 2 — judgement, inside the Planner.** The plan prompt still determines blocking from issue content, and now owns the cases gate 1 structurally cannot see:

- **Parent/child rules.** A sub-issue edge is not a dependency edge. An improve spec is never reported as blocked by its own children, so "a parent cannot be worked while an implementation child is open" is reachable only by reading the `## Parent` declaration.
- **Blockers stated only in prose.** A body that says "needs the parser seam first" without a registered edge.
- **File overlap.** Explicitly not a blocking edge — a scheduling concern the Planner resolves under conflict avoidance.

The Planner is told its candidate list is pre-filtered, so it spends its reasoning on gate 2's cases instead of re-deriving gate 1's.

## What each gate sees

The Planner receives two lists and they are filtered differently:

- The **ready-for-agent candidate set** — issues it may pick — passes through gate 1.
- The **all-open list** — the blocker universe — does not. The plan prompt treats any open issue as a hard blocker regardless of label, so filtering this list would delete blockers from view rather than apply them.

## Placement

Gate 1 runs **after** in-flight selection and **after** the improve-mode gate. Both orderings are deliberate:

- **After in-flight selection.** An issue can acquire a blocker after work on it has started. Abandoning a half-finished branch because a blocker appeared later is worse than finishing it; the gate governs what may *start*. An issue selected as in-flight is exempt.
- **After the improve-mode gate.** Improve mode dispatches when the ready-for-agent set and the in-flight set are both empty. If gate 1 ran first, a backlog that is entirely blocked would read as idle and improve mode would fill the wait by filing more specs — deepening the queue it is waiting on. A blocked backlog is waiting on work someone is already doing, so it counts as not idle.

## Considered Options

- **Judgement only (status quo).** Rejected: it is what produced #2106 and #2107. A model asked to infer dependencies from prose will sometimes infer wrong, and there is no reason to spend inference on a fact GitHub already holds exactly.
- **Mechanical only.** Rejected: sub-issue edges are not dependency edges, so the parent/child unit rule would go dark entirely, and prose-only blockers would be invisible. The improve path writes native edges, but hand-filed and `/to-tickets` issues do not always.
- **Filter inside the GitHub service.** Rejected: `get_open_issues` has callers beyond the Planner, and a transport that silently hides rows is hard to reason about. Scheduling policy belongs in the iteration layer, beside in-flight selection.
- **Pass the blocker count into the plan prompt and let the Planner apply it.** Rejected: it spends context and inference on an exact fact, and leaves the model free to overrule it.

## Consequences

- `issue_dependencies_summary` must survive open-issue normalization. Any future change to the normalized issue shape that drops the field silently disables gate 1 — the Planner would simply see more candidates and no error would be raised.
- Native dependency edges become load-bearing rather than decorative. An issue filed without them is gated by judgement alone, which is the pre-existing behaviour and remains correct, but it is now the weaker of two available paths.
- Issues filed before this ADR may carry body blocker lines with no native edge, and will read as startable until the edges are added. #2106 and #2107 are known instances.
- The plan prompt's blocker section describes a division of labour rather than the whole job. A future edit that restores "determine whether each issue is blocked" as an unqualified instruction re-creates the duplication this ADR removes.
- ADR 0058's "Blocking written twice" stands, and its rationale is now accurate: the body line is for human readers, and the native edge is what gate 1 reads.
