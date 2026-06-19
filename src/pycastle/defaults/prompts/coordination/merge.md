<task>

Merge the following branches into the current branch and summarize the resolution.

</task>

<context>

{{BRANCHES}}

</context>

<workflow>

For each branch:

1. Run `git merge <branch> --no-edit`
2. If there are merge conflicts, resolve them by reading both sides and choosing the correct resolution
3. After resolving conflicts, run `{{CHECKS}}` to verify everything works
4. If tests fail, fix the issues before proceeding to the next branch

After all branches are merged, leave the working tree ready for pycastle to create the merge commit.

</workflow>

<output>

{{EXPECTED_OUTPUT_SHAPE}}

</output>
