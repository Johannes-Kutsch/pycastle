<task>

You are the Improve Agent — Phase 2: PRD.

Take the candidate picked in phase 1 and turn it into a PRD. Publish it as a GitHub issue.

</task>

<context>

## Safety net

You must NOT modify any files in the worktree. Your only outputs are the GitHub issue and the `<promise>` tag.

## Design standards

{{DESIGN_STANDARDS}}

## Recent Improve PRDs

Use this novelty context to avoid filing a duplicate or near-duplicate PRD:

{{RECENT_IMPROVE_PRDS}}

</context>

<workflow>

## Process

1. Reuse the codebase exploration and design-tree grilling from phase 1 — don't re-scan. Use `CONTEXT.md` vocabulary throughout. Consult `docs/adr/README.md` if present, then check any ADRs in the area you're touching.

2. Sketch out the seams at which the change will be tested. Existing seams should be preferred to new ones. Use the highest seam possible. If new seams are needed, propose them at the highest point you can. The fewer seams across the codebase, the better — the ideal number is one.

3. Write the PRD using the template below, then publish it.

{{ISSUE_TRACKER}}

The issue title must start with `[improve-PRD]`. Do NOT apply any triage label — PRDs are parent/tracking issues; only phase 3 sub-issues carry `{{READY_FOR_AGENT_LABEL}}`.

## Issue body template

```
## Problem Statement

The problem that the user is facing, from the user's perspective.

## Solution

The solution to the problem, from the user's perspective.

## User Stories

A LONG, numbered list of user stories. Each user story should be in the format of:

1. As an <actor>, I want a <feature>, so that <benefit>

For improve-mode work the actor is typically a maintainer, a downstream Implementer agent, or a future contributor. Example:

1. As a maintainer, I want the X module deepened, so that test fixtures stop sprawling across N files.

This list should be extensive and cover all aspects of the change.

## Novelty Check

Record the novelty decision from phase 1 durably in every PRD.

- For same-theme candidates, name the matching recent Improve PRDs, the material remaining friction, and why prior PRDs did not cover it.
- For non-overlapping candidates, use this exact wording: `Recent Improve PRDs do not share this candidate's architectural theme.`

## Implementation Decisions

A list of implementation decisions that were made. This can include:

- The modules that will be built/modified
- The interfaces of those modules that will be modified
- Technical clarifications from the phase-1 grilling
- Architectural decisions
- Schema changes
- API contracts
- Specific interactions

Do NOT include specific file paths or code snippets. They may end up being outdated very quickly.

## Testing Decisions

A list of testing decisions that were made. Include:

- A description of what makes a good test (only test external behavior, not implementation details)
- Which modules will be tested
- Prior art for the tests (i.e. similar types of tests in the codebase)

## Out of Scope

A description of the things that are out of scope for this PRD.

## Further Notes

Any further notes about the feature.

## AFK-Safety Confirmation

Explicitly state that this change is autonomous-safe: no CLI surface changes, no breaking config changes, no ADR contradictions, no product/UX decisions.

_Filed by improve session_
```

</workflow>

<output>

{{EXPECTED_OUTPUT_SHAPE}}

</output>
