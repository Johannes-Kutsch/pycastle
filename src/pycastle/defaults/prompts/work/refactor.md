<task>

Fix issue #{{ISSUE_NUMBER}}: {{ISSUE_TITLE}}

{{WORK_SHARED_INSTRUCTIONS}}

## 1. Explore

Explore only the files named in the issue and any files those directly import.

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

{{IMPLEMENT_OUTPUT_RULES}}

</output>
