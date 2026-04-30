# Preflight failure — investigate and file issue

A preflight check has failed. Your job is to investigate the root cause, determine whether human judgment is required, and file a well-structured GitHub issue.

## Failing check

**Name:** {{CHECK_NAME}}

**Command:** `{{COMMAND}}`

**Output:**

```
{{OUTPUT}}
```

## Steps

### 1. Explore the codebase

Do not treat the raw output above as the final answer. Explore the repository to find the true root cause:

- Read the files implicated by the error output
- Check recent commits (`git log --oneline -20`) to see if a recent change introduced the failure
- Reproduce the failure locally if possible by running `{{COMMAND}}`
- Identify whether the failure is a genuine bug in production code, a broken test, a missing dependency, or a configuration problem

### 2. Evaluate HITL

Decide whether this fix requires human judgment. Apply the `{{HITL_LABEL}}` label when:

- The fix requires a design decision about intended behavior
- It is unclear which of several plausible fixes is correct
- The failure could be fixed in multiple incompatible ways and choosing the wrong one would have significant consequences
- You are uncertain

**Default to `{{HITL_LABEL}}` when in doubt.** Only choose `{{ISSUE_LABEL}}` when the fix is unambiguous and does not require guessing at design intent.

### 3. Determine the correct labels

The configured label names are injected into this prompt as placeholders. Use `{{ISSUE_LABEL}}` as the agent-fixable label and `{{HITL_LABEL}}` as the human label.

**Never apply `needs-triage`.** The preflight-issue agent performs triage inline.

**Never band-aid the failure** (e.g. deleting a failing test, weakening an assertion, or adding an exception to make the check pass). The issue body must describe what is genuinely broken.

### 4. File the GitHub issue

Create a GitHub issue with:

- **Title:** `{{CHECK_NAME}} preflight check failed`
- **Body:** structured as shown below (fill in from your investigation):

```
## Root cause

<explain what is broken and why, based on your codebase exploration>

## Intended behavior

<describe what the correct behavior should be>

## Fix prescription

<describe the specific change needed to fix the root cause — be precise enough that an implementer can act on this without further investigation>

## Reproduction

**Command:** `{{COMMAND}}`

**Output:**
\`\`\`
{{OUTPUT}}
\`\`\`
```

Use this command to create the issue, substituting your actual body:

```
gh issue create --title "{{CHECK_NAME}} preflight check failed" --body "$(cat <<'EOF'
<your structured body here>
EOF
)"
```

### 5. Apply labels

Apply the `{{BUG_LABEL}}` label and either the `{{ISSUE_LABEL}}` or `{{HITL_LABEL}}` label to the newly created issue:

```
gh issue edit <number> --add-label "{{BUG_LABEL}}" --add-label "<{{ISSUE_LABEL}} or {{HITL_LABEL}}>"
```

Do **not** apply `needs-triage`.

### 6. Output the issue number and labels

After the issue is filed and labeled, output the details in this exact format:

```
<issue>
{"number": NUMBER, "labels": ["LABEL 1", "LABEL 2"]}
</issue>
```

Where `labels` is the exact list of labels you applied to the issue.
