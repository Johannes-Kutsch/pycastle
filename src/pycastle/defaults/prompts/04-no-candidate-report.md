# TASK

You are the Improve Agent — Phase 4: No-Candidate Report.

Phase 1 found no suitable improvement candidate after applying the AFK-safety filter.

Before filing, run:
`gh issue list --search "[improve-{{IMPROVE_SHORT_SID}}] in:title" --state all --json number,title,labels`
If a no-candidate report already exists for this session, skip filing and emit `<promise>COMPLETE</promise>` immediately.

Otherwise file a single `ready-for-human` issue. The title must start with `[improve-{{IMPROVE_SHORT_SID}}]`.

## Issue Body

Structure the body with these sections:

**Rejected Candidates** — list every shortlisted candidate with the specific reason it was rejected by the AFK-safety filter.

**AFK-Safety Constraints** — describe the constraints that were applied: the forbidden list (CLI surface changes, breaking config changes, scope/architecture choices with multiple defensible answers, ADR contradictions, product/UX calls, issue-tracker contract changes) and the allowed list (internal refactors, deepening shallow modules, test code duplication / fixture-sprawl cleanup, type tightening, naming alignment with `CONTEXT.md`, dead-code removal).

**Session Footer** — end the body with the line:
`_Filed by improve session [improve-{{IMPROVE_SHORT_SID}}]._`

## Output

Emit `<promise>COMPLETE</promise>` when the issue is filed.
