Output your plan as a JSON object wrapped in `<plan>` tags.

The JSON must have two fields:

- `issues`: unblocked {{READY_FOR_AGENT_LABEL}} issues to implement. Use an **empty list** if every candidate is blocked.
- `blocked`: {{READY_FOR_AGENT_LABEL}} issues held back because of a blocker. Each entry should include only:
  - `number`: the blocked issue's number
  - `title`: the blocked issue's title

Example — some unblocked, some blocked:

<plan>
{"issues": [{"number": 42, "title": "Fix auth bug"}], "blocked": [{"number": 43, "title": "Update auth session handling"}]}
</plan>

Example — all issues are blocked:

<plan>
{"issues": [], "blocked": [{"number": 5, "title": "Refresh setup scaffold docs"}]}
</plan>
