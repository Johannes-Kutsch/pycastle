<task>

Resolve the merge conflicts for the following branch and summarize the resolution.

</task>

<context>

{{BRANCHES}}

</context>

<workflow>

A merge is already in progress in this worktree. Resolve all conflicts:

1. Read both sides of each conflict and choose the correct resolution
2. After resolving all conflicts, run `{{CHECKS}}` to verify everything works
3. If tests fail, fix the issues before finishing

Leave the working tree ready for pycastle to create the merge commit.

</workflow>

<output>

{{EXPECTED_OUTPUT_SHAPE}}

</output>
