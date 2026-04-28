# Pre-flight failure report

A pre-flight check failed. File a GitHub issue for it — no diagnosis, no exploration.

## Check

**Name:** {{CHECK_NAME}}

**Command:** `{{COMMAND}}`

**Output:**

```
{{OUTPUT}}
```

## Steps

1. Create a GitHub issue:
   - Title: `[pre-flight] {{CHECK_NAME}} failed`
   - Body: include the command and full output exactly as shown above
   ```
   gh issue create --title "[pre-flight] {{CHECK_NAME}} failed" --body "$(cat <<'EOF'
   ## Pre-flight check failed

   **Command:** \`{{COMMAND}}\`

   **Output:**

   \`\`\`
   {{OUTPUT}}
   \`\`\`
   EOF
   )"
   ```

2. Apply labels to the newly created issue (use the issue number from step 1):
   ```
   gh issue edit <number> --add-label "bug" --add-label "needs-triage"
   ```
