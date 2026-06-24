# Host-owned conflict merge commits

The Merger no longer owns Git history. Pycastle starts one conflict merge per sandbox, asks the Merger to resolve the active branch's already-started merge under the same prompt-and-feedback boundary as Implementer/Reviewer, then pycastle creates the merge commit and validates that the branch tip is an ancestor of the target HEAD before closing the issue. This rejects prompt-only fixes and automatic ancestry-only repair: prompt discipline is not enforcement, and `git merge -s ours` is only acceptable as manual incident recovery after a human verifies content.

## Consequences

- Conflicting branches are recovered one branch at a time in fresh merge-sandbox worktrees based on the current safe SHA.
- The Merger may see sibling conflicting branches as context, but its task is a single active branch.
- Pycastle owns `git merge`, `git add`, `git commit`, branch ancestry validation, branch cleanup, and issue closing.
- Merger output uses the commit-message contract rather than completion-only `<promise>COMPLETE</promise>`.

## Amendment: merge-sandbox never resumes across safe-SHA changes

Issue #1022 exposed that a preserved merge-sandbox can resume conflict resolution from an obsolete broken baseline after an operator repairs `main` and restarts pycastle. The chosen contract is: Merger work is ephemeral merge context, not durable issue work. Every Merger attempt recreates the merge-sandbox from the current safe SHA, even if a prior merge-sandbox contains preserved failure state. This prevents stale conflict resolutions from creating more downstream merge conflicts.
