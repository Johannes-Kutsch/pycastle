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

Read the issue's **What to build** and acceptance criteria to identify the exact markdown edits requested. Explore only the markdown files named in the issue. Do not survey the full repository.

Use the domain glossary in `CONTEXT.md` so that terminology matches the project's language.

## 2. Edit

Apply only the markdown edits described in the issue's **What to build**.

Rules:

- Do not touch any code files (`.py`, `.toml`, `.cfg`, etc.).
- Do not write new tests. Do not add test cases.
- Do not run feedback commands or test suites.

## 3. Output

{{IMPLEMENT_OUTPUT_RULES}}

</workflow>

