# ISSUES

Here are the open issues in the repo:

<issues-json>

{{OPEN_ISSUES_JSON}}

</issues-json>

# TASK

Analyze the open issues and build a dependency graph. For each issue, determine whether it **blocks** or **is blocked by** any other open issue.

An issue B is **blocked by** issue A if:

- B requires code or infrastructure that A introduces
- B and A modify overlapping files or modules, making concurrent work likely to produce merge conflicts
- B's requirements depend on a decision or API shape that A will establish

An issue is **unblocked** if it has zero blocking dependencies on other open issues.

Any issue referenced as a dependency that does not appear in the open issues list above has already been completed. Do not treat absent issues as blockers. Do not infer blockers from integration stability concerns — if a referenced issue is not in the list, its work is fully integrated and stable.

If the issue appears to be a PRD and it has implementation issues which link to it, the PRD cannot be worked on.

# OUTPUT

Output your plan as a JSON object wrapped in `<plan>` tags:

<plan>
{"issues": [{"number": 42, "title": "Fix auth bug"}]}
</plan>

Include only unblocked issues. If every issue is blocked, include the single highest-priority candidate (the one with the fewest or weakest dependencies).

If multiple unblocked issues work on the same part of the codebase, only include the highest priority one to prevent merge conflicts.

When you are not sure which issue has a higher priority, choose the one with the lowest issue number.