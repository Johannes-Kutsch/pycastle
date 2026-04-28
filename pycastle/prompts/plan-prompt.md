# ISSUES

Here are the open issues in the repo:

<issues-json>

!`gh issue list --state open --label {{ISSUE_LABEL}} --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'`

</issues-json>

# TASK

Analyze the open issues and build a dependency graph. For each issue, determine whether it **blocks** or **is blocked by** any other open issue.

An issue B is **blocked by** issue A if:

- B requires code or infrastructure that A introduces
- B and A modify overlapping files or modules, making concurrent work likely to produce merge conflicts
- B's requirements depend on a decision or API shape that A will establish

An issue is **unblocked** if it has zero blocking dependencies on other open issues.

For each unblocked issue, assign a branch name using the format `sandcastle/issue-{number}-{slug}`.

If the issue appears to be a PRD and it has implementation issues which link to it, the PRD cannot be worked on.

# OUTPUT

Output your plan as a JSON object wrapped in `<plan>` tags with two keys:

- `unblocked_issues`: issues ready to work on now (include a `branch` field for each)
- `blocked_issues`: issues that cannot be started yet (omit the `branch` field)

<plan>
{"unblocked_issues": [{"number": 42, "title": "Fix auth bug", "branch": "sandcastle/issue-42-fix-auth-bug"}], "blocked_issues": [{"number": 43, "title": "Add OAuth flow"}]}
</plan>

If every issue is blocked, place the single highest-priority candidate (fewest or weakest dependencies) in `unblocked_issues` and the rest in `blocked_issues`.
