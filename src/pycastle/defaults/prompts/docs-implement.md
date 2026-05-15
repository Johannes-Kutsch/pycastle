# TASK

Fix issue #{{ISSUE_NUMBER}}: {{ISSUE_TITLE}}

Only work on the issue specified. Work on branch {{BRANCH}}.

# CONTEXT

<issue>

{{ISSUE_BODY}}

</issue>

<comments>

{{ISSUE_COMMENTS}}

</comments>
{{WIP_COMMITS}}
# WORKFLOW

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

Do not stage files or run `git commit` — the orchestrator handles commits.

Emit a `<commit_message>` tag with a plain description of the changes. Keep it concise — task completed, key decisions made, and files changed.

Example: `<commit_message>task completed + PRD reference; key decisions; files changed; Blockers or notes for next iteration</commit_message>`

