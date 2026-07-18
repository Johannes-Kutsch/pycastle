<task>

Fix issue #{{ISSUE_NUMBER}}: {{ISSUE_TITLE}}

{{WORK_SHARED_INSTRUCTIONS}}

---

## Explore

Explore only the files mentioned in the issue and the test files that directly touch those files.

From the issue's acceptance criteria, derive an ordered list of behaviors to implement. Most critical paths first, edge cases last.

The seams under test were agreed at PRD time: test only at the observable surfaces the issue's acceptance criteria name. Do not invent new seams in-session.

---

## Per-behavior loop

For each behavior in order:

### 1. RED — write the failing test

Write **one** failing test that confirms the behavior works end-to-end:

```
RED: Write test → run {{FEEDBACK_COMMANDS}} → test fails
```

**Gate rule — forbidden until the first `<behavior>` tag is emitted:**
- `Edit` or `Write` on any non-test file is forbidden.
- Do not touch production source files until the first `<behavior>` tag has been emitted with a real failing-test paste.

### 2. Emit `<behavior>`

Before writing any production code for this behavior, emit:

`<behavior>` — the behavior name, observable surface, test file path, and the real pytest output showing the test red.

```
<behavior>
Behavior name: <name from acceptance criteria>
Observable surface: <what the caller/test observes when the behavior is working>
Test file: <path to the test file>
Failing test output:
<paste real pytest output here — the test must be failing>
</behavior>
```

### 3. GREEN — minimal code

Write the minimal production code to make the test pass:

```
GREEN: Write minimal code to pass → run {{FEEDBACK_COMMANDS}} → test passes
```

Then move to the next behavior.

---

## Stop at green

Refactoring is not part of this session — it belongs to the review stage. When the last behavior is green, you are done.

{{IMPLEMENTATION_STANDARDS}}

</workflow>

<output>

{{EXPECTED_OUTPUT_SHAPE}}

</output>
