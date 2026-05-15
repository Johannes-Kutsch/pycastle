<task>

Fix issue #{{ISSUE_NUMBER}}: {{ISSUE_TITLE}}

Only work on the issue specified. Work on branch {{BRANCH}}.

</task>

<context>

<issue>

{{ISSUE_BODY}}

</issue>

<comments>

{{ISSUE_COMMENTS}}

</comments>
{{WIP_COMMITS}}
</context>

<workflow>

---

## Phase A — Explore + First Failing Test

**You must complete Phase A and emit both required tags before writing any non-test production code.**

### A1. Explore

Explore only the files mentioned in the issue and the test files that directly touch those files. Do not survey the full repository.

Use the domain glossary in `CONTEXT.md` so that test names and interface vocabulary match the project's language. Consult `docs/adr/README.md` if present, then read any ADRs that touch the area you're changing.

### A2. Behaviors

From the issue, derive a prioritized list of behaviors to test. Most critical paths first, edge cases last.

### A3. Tracer Bullet — First Failing Test

Name the first behavior from the acceptance criteria. Write **one** failing test that confirms it works end-to-end:

```
RED: Write test for first behavior → run {{FEEDBACK_COMMANDS}} → test fails
```

**Gate rule — forbidden until both Phase A tags are emitted:**
- `Edit` or `Write` on any non-test file is forbidden.
- Do not touch production source files until `<first_behavior>` and `<failing_test>` have both been emitted.

### A4. Phase A Output (required)

Emit both tags before proceeding to Phase B:

`<first_behavior>` — the behavior name and its observable surface (what a caller observes when the behavior works correctly).

`<failing_test>` — the real pytest output showing the test red (copy the actual terminal output from running `{{FEEDBACK_COMMANDS}}`).

Example shape:

```
<first_behavior>
Behavior name: <name from acceptance criteria>
Observable surface: <what the caller/test observes when the behavior is working>
</first_behavior>

<failing_test>
FAILED tests/test_foo.py::test_bar - AssertionError: ...
[paste real pytest output here]
</failing_test>
```

---

## Phase B — Implement-to-Green Loop

Begin Phase B only after both `<first_behavior>` and `<failing_test>` have been emitted.

### B1. Green — First Behavior

Write the minimal production code to make the first failing test pass:

```
GREEN: Write minimal code to pass → run {{FEEDBACK_COMMANDS}} → test passes
```

### B2. Incremental Loop

For each remaining behavior:

```
RED:   Write next test → fails
GREEN: Minimal code to pass → passes
```

Rules:

- One test at a time
- Only enough code to pass current test
- Don't anticipate future tests
- Keep tests focused on observable behavior
- Run `{{FEEDBACK_COMMANDS}}` after each GREEN

### B3. Refactor

After all tests pass, look for refactor candidates:

- [ ] Apply SOLID principles where natural
- [ ] Run `{{FEEDBACK_COMMANDS}}` after each refactor step

**Never refactor while RED.** Get to GREEN first.

{{IMPLEMENTATION_STANDARDS}}

### B4. Output

Before finishing, run `{{FEEDBACK_COMMANDS}}` to ensure all tests pass.

{{IMPLEMENT_OUTPUT_RULES}}

</workflow>
