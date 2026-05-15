<task>

A preflight check has failed. Investigate the root cause, determine whether human judgment is required, and file a well-structured GitHub issue.

</task>

<context>

## Failing check

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
- Reproduce the failure locally by running `{{COMMAND}}`
- Identify whether the failure is a genuine bug, a broken test, a missing dependency, or a configuration problem

### 2. Evaluate HITL

Decide whether this fix requires human judgment. Apply `{{READY_FOR_HUMAN_LABEL}}` when:

- The fix requires a design decision about intended behavior
- It is unclear which of several plausible fixes is correct
- The failure could be fixed in multiple incompatible ways and choosing the wrong one would have significant consequences
- You are uncertain

**Default to `{{READY_FOR_HUMAN_LABEL}}` when in doubt.** Only choose `{{READY_FOR_AGENT_LABEL}}` when the fix is unambiguous and does not require guessing at design intent.

**Never band-aid the failure** (e.g. deleting a failing test, weakening an assertion, or adding an exception to make the check pass). The issue body must describe what is genuinely broken.

### 3. File the GitHub issue

Create a GitHub issue with:

- **Title:** `{{CHECK_NAME}} preflight check failed`
- **Body:** structured as shown below (fill in from your investigation):

```
## Root cause

<explain what is broken and why, based on your codebase exploration>

## Intended behavior

<describe what the correct behavior should be>

## Fix prescription

<describe the specific change needed to fix the root cause — be precise enough that an implementer can act without further investigation>

## Reproduction

**Command:** `{{COMMAND}}`

**Output:**
\`\`\`
{{OUTPUT}}
\`\`\`
```

Use this command to create the issue:

```
gh issue create --title "{{CHECK_NAME}} preflight check failed" --body "$(cat <<'EOF'
<your structured body here>
EOF
)"
```

### 4. Apply labels

Apply `{{BUG_LABEL}}` and either `{{READY_FOR_AGENT_LABEL}}` or `{{READY_FOR_HUMAN_LABEL}}`. Do **not** apply `needs-triage`.

If you applied `{{READY_FOR_AGENT_LABEL}}`, also apply exactly one slice-mode label:

- `{{BEHAVIOR_SLICE_LABEL}}` — if the fix introduces or changes observable behavior that a new test can verify.
- `{{REFACTOR_SLICE_LABEL}}` — if the fix cannot be verified by a new test of observable behavior (e.g. symbol moves, renames, import rewires, dead-code removal).

**Never apply `{{DOCS_SLICE_LABEL}}`** to a preflight-filed issue.
If you applied `{{READY_FOR_HUMAN_LABEL}}`, do **not** apply any slice-mode label.

```
gh issue edit <number> --add-label "{{BUG_LABEL}}" --add-label "<{{READY_FOR_AGENT_LABEL}} or {{READY_FOR_HUMAN_LABEL}}>" [--add-label "<{{BEHAVIOR_SLICE_LABEL}} or {{REFACTOR_SLICE_LABEL}}>"]
```

### 5. Output the issue number and labels

```
<issue>
{"number": NUMBER, "labels": ["LABEL 1", "LABEL 2"]}
</issue>
```

</workflow>
