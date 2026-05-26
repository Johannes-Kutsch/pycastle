<task>

Fix issue #{{ISSUE_NUMBER}}: {{ISSUE_TITLE}}

Only work on the issue specified. Work on branch {{BRANCH}}.

</task>

<context>

<issue>

{{ISSUE_BODY}}

</issue>

<comments>

{{ISSUE_COMMENTS}}

</comments>
{{WIP_COMMITS}}
</context>

<workflow>

## 1. Explore

Read the issue's **What to build** and acceptance criteria to identify the exact structural change requested. Explore only the files named in the issue and any files those directly import. Do not survey the full repository.

Use the domain glossary in `CONTEXT.md` so that symbol names and vocabulary match the project's language.

## 2. Refactor

Make only the structural change named in the issue. Rename, move, extract, or reshape the symbols listed in the acceptance criteria — nothing else.

Rules:

- Touch only the symbols and files named in the issue.
- Do not add, remove, or change observable behavior.
- Do not write new tests. Do not add test cases.
- Do not test private (`_`-prefixed) helpers — they are implementation details, not interface.
- If the refactor surfaces uncovered behavior, **do not patch it in-session**. File a follow-up `{{BEHAVIOR_SLICE_LABEL}}` issue instead and note it in the commit message.

Run `{{FEEDBACK_COMMANDS}}` after the refactor to confirm all existing tests still pass.

</workflow>

<output>

Before finishing, run `{{FEEDBACK_COMMANDS}}` to ensure all tests pass.

Do not stage files or run `git commit` — the orchestrator handles commits.

Emit a `<commit_message>` tag with a plain description of the changes. Keep it concise — structural change made, files changed, and any follow-up behavior-slice issues filed.

Example: `<commit_message>task completed + PRD reference; key decisions; files changed; Blockers or notes for next iteration</commit_message>`

</output>
