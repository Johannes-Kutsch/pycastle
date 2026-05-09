# ALL OPEN ISSUES

Here are all open issues in the repo (any label), for blocker visibility:

<all-open-issues-json>

{{ALL_OPEN_ISSUES_JSON}}

</all-open-issues-json>

# READY-FOR-AGENT ISSUES

Here are the open issues labeled ready-for-agent — your candidate set to pick from:

<ready-for-agent-issues-json>

{{READY_FOR_AGENT_ISSUES_JSON}}

</ready-for-agent-issues-json>

# TASK

Analyze the open issues and build a dependency graph. For each issue in the ready-for-agent list, determine whether it is **blocked** by any other open issue.

An issue B is **blocked by** issue A if:

- B requires code or infrastructure that A introduces
- B and A modify overlapping files or modules, making concurrent work likely to produce merge conflicts
- B's requirements depend on a decision or API shape that A will establish

## Blocker rules

**Cross-label blockers apply.** Any open issue in the all-open issues list is a hard blocker, regardless of its label. A ready-for-agent issue can be blocked by issues labeled `ready-for-human`, `needs-info`, or `needs-triage` — not only by other ready-for-agent issues.

Only issues labeled `wontfix` are treated as effectively closed. Do not treat `wontfix` issues as blockers.

Any issue referenced as a dependency that does not appear in the open issues list above has already been completed. Do not treat absent issues as blockers. Do not infer blockers from integration stability concerns — if a referenced issue is not in the list, its work is fully integrated and stable.

**Parent PRDs and their implementation issues form a unit.** An implementation issue declares its parent PRD with a `## Parent` heading followed by `#N` near the top of its body. The relationship has two consequences:

- The parent PRD cannot be worked on while any implementation child is open. The child supersedes the spec; the PRD's remaining work is delegated to it.
- An implementation child is **not** blocked by its parent PRD. The PRD's role is to specify the work, and that role is complete the moment the child carries the spec forward. Do not list the parent as a blocker for the child.

## Conflict avoidance

If multiple unblocked issues work on the same part of the codebase, only include the highest-priority one to prevent merge conflicts.

When priority is unclear, choose the one with the lowest issue number.

# OUTPUT

Output your plan as a JSON object wrapped in `<plan>` tags.

The JSON must have two fields:

- `issues`: unblocked ready-for-agent issues to implement. Use an **empty list** if every candidate is blocked.
- `blocked`: ready-for-agent issues held back because of a blocker. Each entry must have:
  - `number`: the blocked issue's number
  - `blocked_by`: the issue number that is blocking it
  - `reason`: a short explanation of the dependency

Example — some unblocked, some blocked:

<plan>
{"issues": [{"number": 42, "title": "Fix auth bug"}], "blocked": [{"number": 43, "blocked_by": 42, "reason": "depends on the auth interface introduced by #42"}]}
</plan>

Example — all issues are blocked:

<plan>
{"issues": [], "blocked": [{"number": 5, "blocked_by": 3, "reason": "requires the user model schema changes from #3 (ready-for-human)"}]}
</plan>
