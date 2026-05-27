<task>

Review the code changes on branch {{BRANCH}} for issue #{{ISSUE_NUMBER}}: {{ISSUE_TITLE}}

**Constraint**: Never change what the code does — only how it does it. All original features, outputs, and behaviors must remain intact.

</task>

<context>

<issue>

{{ISSUE_BODY}}

</issue>

<comments>

{{ISSUE_COMMENTS}}

</comments>
{{INTERRUPTED_WORK}}
</context>

<workflow>

## 1. Confirm baseline

Run `{{FEEDBACK_COMMANDS}}` to confirm the current state passes before making any changes. You will emit `<checks_passed>` at the end of the session with the final summary line from this command output.

## 2. Verify behavior

Attempt to reproduce the original bug with new test cases. If you can reproduce it, the implementation is incomplete — fix it.

## 3. Read the diff

Run `git diff main... --stat` to get the file-level summary, then `git diff main...` (and narrower variants scoped to specific paths) to inspect what changed. For anything that looks suspicious — fragile logic, unchecked assumptions, tricky conditions, implicit type coercions, missing guards — write a test that exercises it. Try to break it. If you can, fix it.

Emit `<reviewed_diff>` immediately after reading so downstream steps are gated on having actually read the diff:

```
<reviewed_diff>
<paste of git diff main... --stat output>
<one-line summary per changed file>
</reviewed_diff>
```

## 4. Enforce test standards

Identify all test files modified in the diff. For each, scan for red flags using the standards below:

{{IMPLEMENTATION_STANDARDS}}

Check that test names use the domain glossary from `CONTEXT.md`.

For each red-flag test:

1. **Refactor first** — rewrite it to exercise the same behavior through the public interface
2. **Delete as last resort** — only if no public behavior can validate it

Run `{{FEEDBACK_COMMANDS}}` after any changes.

## 5. Stress-test edge cases

For every changed code path, probe: empty/zero/null inputs, missing optional fields, off-by-one errors, rapid repeated calls, and adjacent-feature regressions. Write tests for uncovered cases.

## 6. Code quality

Reduce unnecessary complexity and nesting, eliminate redundant code, improve naming clarity, consolidate related logic, remove comments that describe obvious code. Choose clarity over brevity.

## 7. Apply project standards

Consult `docs/adr/README.md` if present, then check that the implementation respects any ADRs in `docs/adr/` that touch the area being changed. Flag violations to fix before committing.

Run `{{FEEDBACK_COMMANDS}}` to ensure nothing is broken.

## 8. Output

Emit the three output tags. `<reviewed_diff>` must already be present from step 3; emit `<checks_passed>` and optionally `<commit_message>` now.

```
<checks_passed>
<final FEEDBACK_COMMANDS summary line>
</checks_passed>
```

```
<commit_message>
Description of refinements (optional — omit if no changes were made)
</commit_message>
```

</workflow>
