<task>

Review issue #{{ISSUE_NUMBER}}: {{ISSUE_TITLE}}

**Constraint**: The issue's spec is the only license to change behavior. Spec-fidelity fixes (step 3) may change what the code does; every other step changes only how it does it — all original features, outputs, and behaviors must remain intact.

{{WORK_SHARED_INSTRUCTIONS}}

## 1. Confirm baseline

Run `{{FEEDBACK_COMMANDS}}` to confirm the current state passes before making any changes. You will emit `<checks_passed>` at the end of the session with the final summary line from this command output.

## 2. Read the diff

Run `git diff main... --stat` to get the file-level summary, then `git diff main...` (and narrower variants scoped to specific paths) to inspect what changed. For anything that looks suspicious — fragile logic, unchecked assumptions, tricky conditions, implicit type coercions, missing guards — write a test that exercises it. Try to break it. If you can, fix it.

Emit `<reviewed_diff>` immediately after reading so downstream steps are gated on having actually read the diff:

```
<reviewed_diff>
<paste of git diff main... --stat output>
<one-line summary per changed file>
</reviewed_diff>
```

## 3. Spec fidelity

Compare the diff against the issue's **What to build** and acceptance criteria:

- **Missing or partial** — a requirement the issue asked for that the diff doesn't deliver → implement it.
- **Scope creep** — behavior in the diff the issue never asked for → remove it.
- **Implemented but wrong** — a requirement that looks done but whose behavior is incorrect → fix it. For bug issues, attempt to reproduce the original bug with new test cases; if you can reproduce it, the implementation is incomplete — fix it.

## 4. Enforce test standards

Identify all test files modified in the diff. For each, scan for red flags using the standards below:

{{IMPLEMENTATION_STANDARDS}}

Check that test names use the domain glossary from `CONTEXT.md`.

Additionally scan every new or changed test for verdicts that depend on anything other than the code under test: hardcoded absolute datetimes compared against a real clock read (time bombs), pre-existing filesystem state, OS-specific behavior (path separators, line endings, tz database), execution order, `sleep()`-based synchronization, or ambient environment. Treat each as a red flag.

For each red-flag test:

1. **Refactor first** — rewrite it to exercise the same behavior through the public interface
2. **Delete as last resort** — only if no public behavior can validate it

Run `{{FEEDBACK_COMMANDS}}` after any changes.

## 5. Stress-test edge cases

For every changed code path, probe: empty/zero/null inputs, missing optional fields, off-by-one errors, rapid repeated calls, and adjacent-feature regressions. Write tests for uncovered cases.

## 6. Code smells

Match the diff against the smell baseline below. Each smell is a judgement call, never a hard violation; a documented project standard overrides the baseline, and skip anything tooling already enforces. Each smell reads *what it is* → *how to fix*; fix the ones you find:

- **Mysterious Name** — a function, variable, or type whose name doesn't reveal what it does or holds. → rename it; if no honest name comes, the design's murky.
- **Duplicated Code** — the same logic shape appears in more than one hunk or file in the change. → extract the shared shape, call it from both.
- **Feature Envy** — a method that reaches into another object's data more than its own. → move the method onto the data it envies.
- **Data Clumps** — the same few fields or params keep travelling together (a type wanting to be born). → bundle them into one type, pass that.
- **Primitive Obsession** — a primitive or string standing in for a domain concept that deserves its own type. → give the concept its own small type.
- **Repeated Switches** — the same `switch`/`if`-cascade on the same type recurs across the change. → replace with polymorphism, or one map both sites share.
- **Shotgun Surgery** — one logical change forces scattered edits across many files in the diff. → gather what changes together into one module.
- **Divergent Change** — one file or module is edited for several unrelated reasons. → split so each module changes for one reason.
- **Speculative Generality** — abstraction, parameters, or hooks added for needs the spec doesn't have. → delete it; inline back until a real need shows.
- **Message Chains** — long `a.b().c().d()` navigation the caller shouldn't depend on. → hide the walk behind one method on the first object.
- **Middle Man** — a class or function that mostly just delegates onward. → cut it, call the real target direct.
- **Refused Bequest** — a subclass or implementer that ignores or overrides most of what it inherits. → drop the inheritance, use composition.

Also deepen or combine shallow modules the diff introduces, and fix existing code the new code reveals as problematic.

Run `{{FEEDBACK_COMMANDS}}` after any changes.

## 7. Apply project standards

Consult `docs/adr/README.md` if present, then check that the implementation respects any ADRs in `docs/adr/` that touch the area being changed. Flag violations to fix before committing.

Run `{{FEEDBACK_COMMANDS}}` to ensure nothing is broken.

</workflow>

<output>

{{EXPECTED_OUTPUT_SHAPE}}

</output>
