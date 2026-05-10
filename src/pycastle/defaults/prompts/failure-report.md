# Failure Analysis

An agent (`{{FAILED_ROLE}}`) failed irrecoverably in this repository. Your job is to file a
single triage issue so a human can investigate and decide how to proceed.

## What failed

The agent role that failed: **`{{FAILED_ROLE}}`**

Its session transcript (if present) is at: `{{SESSION_DIR}}/`

Failure class: `{{FAILURE_CLASS}}`

## Your task

1. Read the session transcript at `{{SESSION_DIR}}/` to understand what the agent attempted
   and where it broke down.
2. Run `git status` and `git diff` to inspect any uncommitted worktree state the failed agent
   left behind.
3. File exactly one GitHub issue in the working repository using `gh issue create` with:
   - Labels: `{{BUG_LABEL}}` and `{{NEEDS_TRIAGE_LABEL}}`
   - A title that names the failed role and concisely describes the failure
   - A body that captures:
     - Which agent role failed
     - The last meaningful output or error from the session transcript
     - The worktree state (uncommitted files, if any)
     - Enough context for a human to reproduce or understand the failure

4. Once the issue is filed, output its number in this exact format:

```
<issue>{"number": ISSUE_NUMBER, "labels": ["{{BUG_LABEL}}", "{{NEEDS_TRIAGE_LABEL}}"]}</issue>
```

Replace `ISSUE_NUMBER` with the integer returned by `gh issue create`.

Do not attempt to fix the failure or run any checks — analysis and filing only.
{{#if FAILURE_CLASS=non_typed_crash}}
## Recovery

The agent crashed mid-session with an untyped exception. If you suspect the session transcript
is corrupted, the human can wipe the session state with:

```
rm -rf <SESSION_DIR>
```

Apply this only if transcript corruption is suspected — it cannot be undone.
{{/if}}
