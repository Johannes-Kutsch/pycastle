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

Decide whether this fix requires human judgment. Apply the `ready-for-human` label when:

- The fix requires a design decision about intended behavior
- It is unclear which of several plausible fixes is correct
- The failure could be fixed in multiple incompatible ways and choosing the wrong one would have significant consequences
- You are uncertain

**Default to `ready-for-human` when in doubt.** Only choose `ready-for-agent` when the fix is unambiguous and does not require guessing at design intent.

### 3. Determine the correct labels

Read the project configuration to find the configured label names — do not hardcode strings. The config file is at `pycastle/config.py`. Use the value of `ISSUE_LABEL` as the agent-fixable label. The human label is `ready-for-human`.

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

Apply the `bug` label and either the `ready-for-agent` or `ready-for-human` label (the exact label names from config) to the newly created issue:

```
gh issue edit <number> --add-label "bug" --add-label "<ready-for-agent or ready-for-human>"
```

Do **not** apply `needs-triage`.

### 6. Output the issue number

After the issue is filed and labeled, output the issue number in this exact format:

```
<issue>NUMBER</issue>
```
