<task>

Fix issue #{{ISSUE_NUMBER}}: {{ISSUE_TITLE}}

{{WORK_SHARED_INSTRUCTIONS}}

---

## Explore

Explore only the files mentioned in the issue and the test files that directly touch those files.

From the issue's acceptance criteria, derive an ordered list of behaviors to implement. Most critical paths first, edge cases last.

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

## Refactor

After all behaviors are green, look for refactor candidates:

- [ ] Apply SOLID principles where natural
- [ ] Run `{{FEEDBACK_COMMANDS}}` after each refactor step

**Never refactor while RED.** Get to GREEN first.

{{IMPLEMENTATION_STANDARDS}}

</workflow>

<output>

### Output

Before finishing, run `{{FEEDBACK_COMMANDS}}` to ensure all tests pass.

{{IMPLEMENT_OUTPUT_RULES}}

</output>
