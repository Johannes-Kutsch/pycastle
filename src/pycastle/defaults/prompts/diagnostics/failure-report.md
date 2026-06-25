<task>

An agent (`{{FAILED_ROLE}}`) failed irrecoverably. File a triage issue so a human can investigate.

</task>

<context>

## What failed

Role: **`{{FAILED_ROLE}}`**

Invocation evidence: `{{EVIDENCE_PATH}}`

{{#if HAS_EVIDENCE_PATH=no}}
No invocation log was copied for diagnosis. Rely on the worktree state:
- Run `git status`
- Run `git diff`
{{/if}}

Failure class: `{{FAILURE_CLASS}}`

</context>

<workflow>

## Your task

1. Read the invocation evidence at `{{EVIDENCE_PATH}}` to understand what the failed role attempted and where it broke down.
2. Run `git status` and `git diff` to inspect any uncommitted worktree state the failed agent left behind.
3. File exactly one GitHub issue with:
   - Labels: `{{BUG_LABEL}}` and `{{NEEDS_TRIAGE_LABEL}}`
   - A title that names the failed role and concisely describes the failure
   - A body that captures: which role failed, the last meaningful output or error, worktree state, and enough context to reproduce or understand the failure

{{ISSUE_TRACKER}}

Do not attempt to fix the failure — analysis and filing only.
{{#if FAILURE_CLASS=non_typed_crash}}
## Recovery

The agent crashed mid-session with an untyped exception. If you suspect the session transcript is corrupted, the human can wipe the session state with:

```
rm -rf <SESSION_DIR>
```

Apply this only if transcript corruption is suspected — it cannot be undone.
{{/if}}

</workflow>

<output>

{{EXPECTED_OUTPUT_SHAPE}}

</output>
