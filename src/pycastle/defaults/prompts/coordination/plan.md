<task>

Analyze the open issues and build a dependency graph. For each issue in the {{READY_FOR_AGENT_LABEL}} list, determine whether it is **blocked** by any other open issue.

</task>

<context>

Here are all open issues in the repo (any label), for blocker visibility:

<all-open-issues-json>

{{ALL_OPEN_ISSUES_JSON}}

</all-open-issues-json>

Here are the open issues labeled {{READY_FOR_AGENT_LABEL}} — your candidate set to pick from:

<ready-for-agent-issues-json>

{{READY_FOR_AGENT_ISSUES_JSON}}

</ready-for-agent-issues-json>

</context>

<workflow>

When blocker analysis requires architectural context, use `Read` to selectively read `CONTEXT.md` and files under `docs/adr/`.

An issue B is **blocked by** issue A if:

- B requires code or infrastructure that A introduces
- B and A modify overlapping files or modules, making concurrent work likely to produce merge conflicts
- B's requirements depend on a decision or API shape that A will establish

## Blocker rules

**Cross-label blockers apply.** Any open issue is a hard blocker regardless of label. A {{READY_FOR_AGENT_LABEL}} issue can be blocked by `{{READY_FOR_HUMAN_LABEL}}`, `{{NEEDS_INFO_LABEL}}`, or `{{NEEDS_TRIAGE_LABEL}}` issues.

Only `{{WONTFIX_LABEL}}` issues are treated as effectively closed. Do not treat `{{WONTFIX_LABEL}}` issues as blockers.

Issues absent from both lists have already been completed. Do not treat absent issues as blockers.

**Parent PRDs and their implementation issues form a unit.** An implementation issue declares its parent PRD with a `## Parent` heading followed by `#N` near the top of its body. The relationship has two consequences:

- The parent PRD cannot be worked on while any implementation child is open.
- An implementation child is **not** blocked by its parent PRD — the PRD's role is complete once the child carries the spec forward.

## Conflict avoidance

If multiple unblocked issues work on the same part of the codebase, only include the highest-priority one to prevent merge conflicts. When priority is unclear, choose the lowest issue number.

</workflow>

<output>

{{EXPECTED_OUTPUT_SHAPE}}

</output>
