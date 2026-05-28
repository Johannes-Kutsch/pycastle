# Host-owned conflict merge commits

The Merger no longer owns Git history. Pycastle starts one conflict merge per sandbox, asks the Merger to resolve the active branch's already-started merge under the same prompt-and-feedback boundary as Implementer/Reviewer, then pycastle creates the merge commit and validates that the branch tip is an ancestor of the target HEAD before closing the issue. This rejects prompt-only fixes and automatic ancestry-only repair: prompt discipline is not enforcement, and `git merge -s ours` is only acceptable as manual incident recovery after a human verifies content.

## Consequences

- Conflicting branches are recovered one branch at a time in fresh merge-sandbox worktrees based on current target HEAD.
- The Merger may see sibling conflicting branches as context, but its task is a single active branch.
- Pycastle owns `git merge`, `git add`, `git commit`, branch ancestry validation, branch cleanup, and issue closing.
- Merger output uses the commit-message contract rather than completion-only `<promise>COMPLETE</promise>`.
