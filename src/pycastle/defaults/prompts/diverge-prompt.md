<task>

Local `{{BRANCH}}` and `origin/{{BRANCH}}` have diverged with textual conflicts that cannot be auto-resolved. Merge `origin/{{BRANCH}}` into the current branch, resolve all conflicts, and produce a clean commit.

</task>

<workflow>

1. Run `git fetch origin` to ensure `origin/{{BRANCH}}` is up to date
2. Run `git merge origin/{{BRANCH}} --no-edit` to start the merge
3. For each conflicted file, read both sides and choose the correct resolution — never leave conflict markers in the tree
4. Stage resolved files with `git add <file>`
5. Run `{{CHECKS}}` to verify everything still passes
6. Commit: `git commit -m "Merge origin/{{BRANCH}} — resolve divergence"`

</workflow>

<output>

Once complete, output `<promise>COMPLETE</promise>`.

If you cannot produce a clean resolution (e.g. the conflict requires a design decision), output `<promise>FAILED</promise>`.

</output>
