<task>

A host check has failed. Investigate the root cause, determine whether human judgment is required, deduplicate against existing host-check issues, and file a well-structured GitHub issue only if the failure is not already covered.

</task>

<context>

This failure was observed by `pycastle check` on the host OS outside the agent container.

## Failing host check

**Host OS:** {{HOST_OS}}

**Host platform:** `{{HOST_PLATFORM}}`

**Checked SHA:** `{{CHECKED_SHA}}`

**Name:** {{CHECK_NAME}}

**Command:** `{{COMMAND}}`

**Output:**

```
{{OUTPUT}}
```

</context>

<workflow>

## Steps

### 1. Explore the codebase

Do not treat the raw output above as the final answer. Explore the repository to find the true root cause:

- Read the files implicated by the error output
- Check recent commits (`git log --oneline -20`) to see if a recent change introduced the failure
- Reproduce the failure on the host by running `{{COMMAND}}`
- Identify whether the failure is a genuine bug, a broken test, a missing dependency, a platform-specific bug, or a host configuration problem

### 2. Deduplicate before filing

Before filing a new host-check issue, deduplicate against existing open host-check issues for the same underlying failure. Search for an existing open issue that already covers the same host OS/platform, check name, and root cause. If one exists, do not file a duplicate; return its issue number instead.

### 3. Evaluate HITL

Decide whether this fix requires human judgment. Apply `{{READY_FOR_HUMAN_LABEL}}` when:

- The fix requires a design decision about intended behavior
- It is unclear which of several plausible fixes is correct
- The failure could be fixed in multiple incompatible ways and choosing the wrong one would have significant consequences
- You are uncertain

**Default to `{{READY_FOR_HUMAN_LABEL}}` when in doubt.** Only choose `{{READY_FOR_AGENT_LABEL}}` when the fix is unambiguous and does not require guessing at design intent.

**Never band-aid the failure** (e.g. deleting a failing test, weakening an assertion, or adding an exception to make the check pass). The issue body must describe what is genuinely broken.

### 4. File the GitHub issue

Create a GitHub issue with:

- **Title:** `{{CHECK_NAME}} host check failed on {{HOST_OS}}`
- **Body:** structured as shown below (fill in from your investigation):

```
## Root cause

<explain what is broken and why, based on your codebase exploration>

## Intended behavior

<describe what the correct behavior should be on this host OS/platform>

## Fix prescription

<describe the specific change needed to fix the root cause — be precise enough that an implementer can act without further investigation>

## Host reproduction

**Host OS:** {{HOST_OS}}

**Host platform:** `{{HOST_PLATFORM}}`

**Checked SHA:** `{{CHECKED_SHA}}`

**Command:** `{{COMMAND}}`

**Output:**
\`\`\`
{{OUTPUT}}
\`\`\`
```

{{ISSUE_TRACKER}}

### 5. Apply labels

Apply `{{BUG_LABEL}}` and either `{{READY_FOR_AGENT_LABEL}}` or `{{READY_FOR_HUMAN_LABEL}}`. Do **not** apply `needs-triage`.

If you applied `{{READY_FOR_AGENT_LABEL}}`, also apply exactly one slice-mode label:

- `{{BEHAVIOR_SLICE_LABEL}}` — if the fix introduces or changes observable behavior that a new test can verify.
- `{{REFACTOR_SLICE_LABEL}}` — if the fix cannot be verified by a new test of observable behavior (e.g. symbol moves, renames, import rewires, dead-code removal).

**Never apply `{{DOCS_SLICE_LABEL}}`** to a host-check-filed issue.
If you applied `{{READY_FOR_HUMAN_LABEL}}`, do **not** apply any slice-mode label.

{{ISSUE_TRACKER}}

</workflow>

<output>

{{EXPECTED_OUTPUT_SHAPE}}

</output>
