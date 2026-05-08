# TASK

You are the Improve Agent — Phase 2: PRD.

Take the candidate picked in phase 1 and turn it into a PRD. Publish it as a GitHub issue.

## Safety net

You must NOT modify any files in the worktree. Your only outputs are the GitHub issue you file via `gh` and the `<promise>` tag.

## Dedup check

Before filing, run:
`gh issue list --search "[improve-{{IMPROVE_SHORT_SID}}] in:title" --state all --json number,title,labels`
If a PRD issue already exists for this session, skip filing and emit `<promise>COMPLETE</promise>` immediately.

## Process

1. Reuse the codebase exploration and design-tree grilling already done in phase 1 — don't re-scan. Use `CONTEXT.md` vocabulary throughout the PRD, and respect any ADRs in the area you're touching.

2. Sketch out the major modules you will need to build or modify to complete the implementation. Actively look for opportunities to extract deep modules that can be tested in isolation.

A deep module (as opposed to a shallow module) is one which encapsulates a lot of functionality in a simple, testable interface which rarely changes.

3. Write the PRD using the template below, then publish it. **Always write the body to a temp file and use `gh issue create --body-file` — never pass the body inline, as PRD content breaks shell quoting.**

The issue title must start with `[improve-{{IMPROVE_SHORT_SID}}]`. Do NOT apply any triage label — the PRD is a parent/tracking issue; only phase 3 sub-issues carry `ready-for-agent`.

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

## Implementation Decisions

A list of implementation decisions that were made. This can include:

- The modules that will be built/modified
- The interfaces of those modules that will be modified
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

_Filed by improve session [improve-{{IMPROVE_SHORT_SID}}]._
```

## Output

Output the filed issue number as `<issue>N</issue>`.

Then emit `<promise>COMPLETE</promise>`.
