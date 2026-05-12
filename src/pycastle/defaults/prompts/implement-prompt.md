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

# WORKFLOW

## 1. Explore

Explore only the files mentioned in the issue and the test files that directly touch those files. Do not survey the full repository.

Use the domain glossary in `CONTEXT.md` so that test names and interface vocabulary match the project's language. Consult `docs/adr/README.md` if present, then read any ADRs that touch the area you're changing.

## 2. Behaviors

From the issue, derive a prioritized list of behaviors to test. Most critical paths first, edge cases last.

{{IMPLEMENTATION_STANDARDS}}

## 3. Tracer Bullet

Take the first behavior from your list. Write ONE test that confirms it works end-to-end:

```
RED:   Write test for first behavior → test fails
GREEN: Write minimal code to pass → test passes
```

## 4. Incremental Loop

For each remaining behavior:

```
RED:   Write next test → fails
GREEN: Minimal code to pass → passes
```

Rules:

- One test at a time
- Only enough code to pass current test
- Don't anticipate future tests
- Keep tests focused on observable behavior
- Run `{{FEEDBACK_COMMANDS}}` after each GREEN

## 5. Refactor

After all tests pass, look for refactor candidates:

- [ ] Apply SOLID principles where natural
- [ ] Run `{{FEEDBACK_COMMANDS}}` after each refactor step

**Never refactor while RED.** Get to GREEN first.

## 6. Output

Before finishing, run `{{FEEDBACK_COMMANDS}}` to ensure all tests pass.

Do not stage files or run `git commit` — the orchestrator handles commits.

Emit a `<commit_message>` tag with a plain description of the changes. Keep it concise — task completed, key decisions made, and files changed.

Example: `<commit_message>task completed + PRD reference; key decisions; files changed; Blockers or notes for next iteration</commit_message>`
