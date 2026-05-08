# TASK

You are the Improve Agent — Phase 2: PRD.

Before filing, run:
`gh issue list --search "[improve-{{IMPROVE_SHORT_SID}}] in:title" --state all --json number,title,labels`
If a PRD issue already exists for this session, skip filing and emit `<promise>COMPLETE</promise>` immediately.

Otherwise write a concise PRD for the chosen improvement and file it as a GitHub issue labeled `ready-for-agent`.
The issue title must start with `[improve-{{IMPROVE_SHORT_SID}}]`.

Include in the issue body:
- A clear problem statement
- The proposed solution
- Acceptance criteria
- Files likely to change

Output the issue number as `<issue>{"number": N, "labels": ["ready-for-agent"]}</issue>`.

Then emit `<promise>COMPLETE</promise>`.
