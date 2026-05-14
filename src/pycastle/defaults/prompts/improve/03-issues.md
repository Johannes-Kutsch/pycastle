# TASK

You are the Improve Agent — Phase 3: Sub-issues.

Break the PRD filed in phase 2 (#{{ISSUE_NUMBER}}: {{ISSUE_TITLE}}) into independently-grabbable issues using vertical slices (tracer bullets).

# CONTEXT

<issue>

{{ISSUE_BODY}}

</issue>

<comments>

{{ISSUE_COMMENTS}}

</comments>

# PROCESS

## Safety net

You must NOT modify any files in the worktree. Your only outputs are the GitHub issues and the `<promise>` tag. CONTEXT.md additions/edits are filed as a dedicated issue (see step 1 below) — never edited in place from this phase.

## 1. Explore

Starting from the PRD above:

- Read `CONTEXT.md` (and `CONTEXT-MAP.md` if present) to ground yourself in the domain vocabulary.
- Consult `docs/adr/README.md` if present, then read relevant ADRs in `docs/adr/` for the PRD area.
- Read the modules the PRD names to understand their current interfaces.

## 2. Detect CONTEXT.md updates

Check whether the candidate introduces a new domain term, sharpens a fuzzy term, or implies an update to `CONTEXT.md`.

If yes, file a single dedicated CONTEXT.md issue **first** before any vertical slice:

- Spell out the **exact additions or edits** in the body, ready for an Implementer to apply verbatim.
- Mark it highest priority — every other sub-issue lists it in its `Blocked by` field.
- Use the same title prefix and labels as the slice issues.

## 3. Draft vertical slices

Each slice cuts through ALL integration layers end-to-end, not a horizontal slice of one layer.

In improve mode every slice must be AFK by construction — the AFK-safety filter was applied in phase 1.

Rules:

- Each slice delivers a narrow but COMPLETE path through every layer (schema, API, UI, tests)
- A completed slice is verifiable on its own
- Prefer many thin slices over few thick ones

### Granularity check

Before approving each slice:

1. **Layer count** — Does the slice touch more than one independently-shippable layer? If yes, sequence as separate slices unless genuinely indivisible.
2. **Read-budget** — Would a fresh agent need to read more than ~5 files outside those being modified? If yes, the slice bundles unrelated context — split it.

Each issue must fit in one usage window of an AFK agent; over-scoping is wasteful.

## 4. Self-quiz

Before filing, answer:

- Is the granularity right? (too coarse / too fine)
- Are the dependency relationships correct?
- Should any slices be merged or split further?
- Is every slice genuinely AFK-implementable?

## 5. File the issues

For each approved slice, publish a new issue. **Always write the body to a temp file and use `gh issue create --body-file`.** Each title must start with `[improve-SLICE]`. Apply the `{{READY_FOR_AGENT_LABEL}}` label.

Publish in dependency order (blockers first) so you can reference real issue identifiers in `Blocked by`. The CONTEXT.md issue from step 2, if any, is filed first.

## Sub-issue body template

```
## Parent

A reference to the parent PRD issue (#N from phase 2).

## What to build

A concise description of this vertical slice. Describe the end-to-end behavior, not layer-by-layer implementation.

## Acceptance criteria

- [ ] Criterion 1
- [ ] Criterion 2
- [ ] Criterion 3

## Blocked by

- A reference to the blocking ticket (if any)

Or "None - can start immediately" if no blockers.

## AFK-Safety Confirmation

Explicitly state that this slice is autonomous-safe: no CLI surface changes, no breaking config changes, no ADR contradictions, no product/UX decisions.

_Filed by improve session_
```

## Sub-issue registration

After creating all issues, register each new issue as a sub-issue of the parent PRD:

`gh api repos/{owner}/{repo}/issues/{parent_number}/sub_issues --method POST --field sub_issue_id={new_issue_id}`

Use `gh repo view --json nameWithOwner -q .nameWithOwner` to obtain `{owner}/{repo}`. Get each child's integer `id` via `gh api repos/{owner}/{repo}/issues/{number} --jq '.id'` before calling the sub-issue endpoint.

Do NOT close or modify the parent PRD issue.

## Output

Output each filed issue number as `<issue>N</issue>`.

Then emit `<promise>COMPLETE</promise>`.
