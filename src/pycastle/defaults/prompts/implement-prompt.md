# Workflow

### 0. Prior Run Detection

Before starting, check whether prior RALPH work exists on this branch.

Run `git log main..HEAD --oneline`. If any commits are present, prior RALPH work is already done — emit `<promise>COMPLETE</promise>` and stop.

Otherwise, run `git status`. If the working tree is dirty, review the existing uncommitted changes and continue from the current state rather than starting over.

If both checks show a clean, empty branch, fall through to step 1 and proceed normally.

### 1. Task

Fix issue #{{ISSUE_NUMBER}}: {{ISSUE_TITLE}}

Pull in the issue using `gh issue view`, with comments. If it has a parent PRD, pull that in too.

Only work on the issue specified. Work on branch {{BRANCH}}.

### 2. Exploration

Explore only the files mentioned in the issue body and the test files that directly touch those files. Do not survey the full repository.

Use the domain glossary in `CONTEXT.md` so that test names and interface vocabulary match the project's language. Respect any ADRs that touch the area you're changing.

### 3. Behaviors

From the issue, derive a prioritized list of behaviors to test. Most critical paths first, edge cases last.

**You can't test everything.** Focus on critical paths and complex logic — not every possible edge case.

Before writing any code, consider the following interface design and deep module guidelines:

{{INTERFACES_STANDARDS}}

{{DEEP_MODULES_STANDARDS}}

### 4. Tracer Bullet

Take the first behavior from your list. Write ONE test that confirms it works end-to-end:

```
RED:   Write test for first behavior → test fails
GREEN: Write minimal code to pass → test passes
```

This is your tracer bullet — choose the simplest test that proves the full path is wired up correctly.

### 5. Incremental Loop

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

{{TESTING_STANDARDS}}

{{MOCKING_STANDARDS}}

Checklist per cycle:

```
[ ] Test describes behavior, not implementation
[ ] Test uses public interface only
[ ] Test would survive internal refactor
[ ] Code is minimal for this test
[ ] No speculative features added
```

### 6. Refactor

After all tests pass, look for refactor candidates:

{{REFACTORING_STANDARDS}}

- [ ] Apply SOLID principles where natural
- [ ] Run `{{FEEDBACK_COMMANDS}}` after each refactor step

**Never refactor while RED.** Get to GREEN first.

### 7. Commit

Before committing, run `{{FEEDBACK_COMMANDS}}` to ensure all tests pass.

Make a git commit. The commit message must:

1. Start with `RALPH:` prefix
2. Include task completed + PRD reference
3. Key decisions made
4. Files changed
5. Blockers or notes for next iteration

Keep it concise.

### 8. Issue

If the task is not complete, leave a comment on the GitHub issue with what was done.

Do not close the issue — this will be done later.

Once complete, output <promise>COMPLETE</promise>.
