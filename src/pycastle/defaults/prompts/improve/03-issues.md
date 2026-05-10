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

You must NOT modify any files in the worktree. Your only outputs are the GitHub issues you file via `gh` and the `<promise>` tag. CONTEXT.md additions/edits are filed as a dedicated issue (see step 1 below) — never edited in place from this phase.

## 1. Explore

Re-explore the area before filing. Starting from the PRD above:

- Read `CONTEXT.md` (and `CONTEXT-MAP.md` if present) to ground yourself in the domain vocabulary.
- Skim ADRs in `docs/adr/` that touch the area described in the PRD.
- Read the modules the PRD names — understand their current interfaces before proposing slices.

## 2. Detect CONTEXT.md updates

Check whether the picked candidate introduces a new domain term, sharpens a fuzzy term, or otherwise implies an update to `CONTEXT.md` (or the per-context glossary referenced from `CONTEXT-MAP.md`).

If yes, file a single dedicated CONTEXT.md issue **first** before any vertical slice. This issue must:

- Spell out the **exact additions or edits** to `CONTEXT.md` in the issue body, ready for an Implementer to apply verbatim.
- Be the highest priority. Every other sub-issue in this session lists this issue in its `Blocked by` field.
- Use the same title prefix and labels as the slice issues below.

If no CONTEXT.md update is implied, skip this step.

## 3. Draft vertical slices

Each slice is a thin vertical slice that cuts through ALL integration layers end-to-end, NOT a horizontal slice of one layer.

The `to-issues` discipline distinguishes HITL (human-in-the-loop) and AFK (autonomous) slices. **In improve mode every slice must be AFK by construction** — the AFK-safety filter has already been applied in phase 1. If a slice cannot be made AFK, the candidate should not have survived phase 1; escalate via NO-CANDIDATE on a future run rather than filing a HITL slice here.

Vertical-slice rules:

- Each slice delivers a narrow but COMPLETE path through every layer (schema, API, UI, tests)
- A completed slice is demoable or verifiable on its own
- Prefer many thin slices over few thick ones

### Granularity check

Before approving each slice, answer these two explicitly:

1. **Layer count** — Does the slice touch more than one independently-shippable architectural layer of the codebase? Identify the layers from `CONTEXT.md` and the module structure. If yes, sequence the layer changes as separate slices, each shipping a working intermediate state — unless the layer changes are genuinely indivisible (one cannot merge without the other compiling / passing tests).

2. **Read-budget** — To implement this slice, would a fresh agent plausibly need to read more than ~5 files outside the ones being modified? If yes, the slice is bundling unrelated context.

A slice that fails either check is a split candidate. The underlying constraint: each issue must fit in one usage window of an AFK agent. Work invested in a session that hits its limit before producing committable output is lost on resume — over-scoping is wasteful, not just inelegant.

## 4. Self-quiz

Before filing, answer the following questions explicitly in the conversation:

- Does the granularity feel right? (too coarse / too fine)
- Are the dependency relationships correct?
- Should any slices be merged or split further?
- Is every slice genuinely AFK-implementable?
- Does each slice pass the granularity check?

Iterate the breakdown in your reasoning until the answers are clean.

## 5. File the issues

For each approved slice, publish a new issue. **Always write the body to a temp file and use `gh issue create --body-file`.** Each title must start with `[auto-generated-improve]`. Apply the `{{READY_FOR_AGENT_LABEL}}` label so an agent can start work on the issue.

Publish issues in dependency order (blockers first) so you can reference real issue identifiers in the `Blocked by` field. The CONTEXT.md issue from step 2, if any, is filed first; every other slice references it.

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

After creating all issues, register each new issue as a sub-issue of the parent PRD using the GitHub API:

`gh api repos/{owner}/{repo}/issues/{parent_number}/sub_issues --method POST --field sub_issue_id={new_issue_id}`

Use `gh repo view --json nameWithOwner -q .nameWithOwner` to obtain `{owner}/{repo}`. Get each child's integer `id` (not number) via `gh api repos/{owner}/{repo}/issues/{number} --jq '.id'` before calling the sub-issue endpoint.

Do NOT close or modify the parent PRD issue.

## Output

Output each filed issue number as `<issue>N</issue>`.

Then emit `<promise>COMPLETE</promise>`.
