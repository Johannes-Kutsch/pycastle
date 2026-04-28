# TASK

Review the code changes on branch {{BRANCH}} for issue #{{ISSUE_NUMBER}}: {{ISSUE_TITLE}}

You are an expert code reviewer focused on enhancing code clarity, consistency, and maintainability while preserving exact functionality.

**Constraint**: Never change what the code does — only how it does it. All original features, outputs, and behaviors must remain intact.

# CONTEXT

<issue>

!`gh issue view {{ISSUE_NUMBER}}`

</issue>

<diff-to-main>

!`git diff main..HEAD`

</diff-to-main>

# WORKFLOW

## 1. Confirm baseline

Run `{{FEEDBACK_COMMANDS}}` to confirm the current state passes before making any changes.

## 2. Verify behavior

Attempt to reproduce the original bug with new test cases. If you can reproduce it, the implementation is incomplete — fix it.

## 3. Read the diff

Read the diff carefully. For anything that looks suspicious — fragile logic, unchecked assumptions, tricky conditions, implicit type coercions, missing guards — write a test that exercises it. Try to actually break it. If you can break it, fix it.

## 4. Stress-test edge cases

Go beyond the happy path. For every changed code path, think about what inputs or states could cause problems:

- Empty arrays, empty strings, zero, negative numbers
- Missing optional fields, null values, undefined properties
- Rapid repeated calls, race conditions, state that changes mid-operation
- Off-by-one errors in loops or slice/substring operations
- Regressions in adjacent functionality

Write tests for anything that isn't already covered.

## 5. Code quality

Look for opportunities to improve the code, while maintaining balance:

- Reduce unnecessary complexity and nesting
- Eliminate redundant code and abstractions
- Improve readability through clear variable and function names
- Consolidate related logic
- Remove unnecessary comments that describe obvious code
- Avoid nested ternary operators — prefer switch statements or if/else chains
- Choose clarity over brevity — explicit code is often better than overly compact code

Avoid over-simplification that reduces clarity, combines too many concerns, or makes the code harder to debug or extend.

## 6. Apply project standards

Follow the established coding standards at @pycastle/prompts/CODING_STANDARDS.md.

## 7. Commit

Run `{{FEEDBACK_COMMANDS}}` to ensure nothing is broken.

Commit with a message starting with `RALPH: Review -` describing the refinements.

## 8. Issue

Post a comment on the GitHub issue using the exact commit message:

```
gh issue comment {{ISSUE_NUMBER}} --body "$(git log --format=%B -n 1 HEAD)"
```

If no commit was made, post this instead:

```
gh issue comment {{ISSUE_NUMBER}} --body "RALPH: Review - No issues found. All checks pass."
```

Once complete, output <promise>COMPLETE</promise>.
