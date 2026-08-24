# An issue branch's base belongs to the run that uses it

Status: accepted.

Two questions about a **durable issue branch** are now asked of the **operating branch** rather than of `main` or the repo root's HEAD, and one new rule follows from them: a branch that carries no commits of its own starts at the SHA of the run that is about to use it, not at the tip it was cut from.

- **Keep or delete at teardown** asks whether the branch holds commits the operating branch does not, from the repo root, instead of counting `main..HEAD` inside the worktree.
- **Reuse** re-points a branch with no work of its own at the run's planner SHA before the Implementer sees it.

## Why

ADR 0060 withdrew every read of the repo root's checked-out branch, and #2195 and #2196 converted the last two ancestry gates it had missed. Two sites in the same family survived both, because neither reads the *checkout* — they read the literal branch `main`:

```python
# infrastructure/worktree.py, before
_branch_has_commits = deps.git_svc.has_commits_ahead_of_main(path)   # git rev-list --count main..HEAD
```

With a **working branch** configured, `main` is dozens of commits behind the operating branch, so a *brand-new empty* issue branch counts as carrying work and is kept at teardown. That is not a slow leak of disk; it is a leak of a **base**. A branch kept this way stays pinned to the operating-branch tip of the run that created it.

`GitService.create_worktree` then completes the failure: when the branch already exists it runs `git worktree add <path> <branch>` and drops the `sha` argument, so the planner SHA threaded from `durable_issue_worktree(planner_sha=…)` has no effect on a reused branch. The Implementer works on a tree missing every merge since, and the result can never fast-forward back into the operating branch.

The observed cost is not the extra merge commit. It is that a stale base makes the merge phase summon a **Merger** agent for a conflict that only exists because of the stale base — and that agent resolves the conflict with the branch's older assumptions in hand. Merging `pycastle/issue-2196` reverted #2195's own decision that `is_ancestor` takes a required comparison target, because #2196 had been written against a base that predated it, and no test named the requirement. A conflict born of a stale base is a conflict an agent can resolve *backwards*.

## Decision detail

- **The teardown question is "ahead of the operating branch".** `has_commits_ahead_of_main` and `count_commits_ahead` are deleted rather than re-parameterised; `branch_has_commits_ahead_of_merge_base` already asks the right question and takes its base branch as a required argument. Asking it from the repo root also removes the ordering constraint that the old form had, where the count had to be taken before the worktree was removed.
- **A reused branch with no work of its own is hard-reset to the requested SHA.** The guard is the same predicate as the teardown question, so the reset can only ever run when there is nothing to lose. It fires only where the worktree is actually being (re)created: a reusable worktree — one still holding a role session dir — keeps its base, which is what resumption requires.
- **`GitService.is_ancestor` takes its comparison target as a required argument**, restoring #2195's decision, with the test that its absence let a merge resolution undo.
- **`select_in_flight_issues` takes its operating branch as a required argument.** Its `"main"` default was correct at no call site.

## Considered options

- **Merge or rebase the operating branch into the stale issue branch** instead of resetting it. Rejected: it is the same operation in the only case that can arise here — the branch is an ancestor, so there is nothing to replay — but it needs a working tree, can fail, and invites the reader to imagine it handles the case where the branch *does* carry work. Reset says plainly that this path only touches branches with nothing on them.
- **Delete the branch and re-cut it.** Rejected as strictly more work than moving the ref, and it would discard the branch's reflog.
- **Leave the reuse path alone and rely on the teardown fix.** Rejected: it would be correct for branches created from here on, and wrong for every branch already leaked by an earlier version, including under an operator's manual worktree removal.
- **Give `_WorktreeDeps` the whole `Config`.** Rejected again, for the reason #2196 gave: the operating branch threads through two signatures, a config protocol widens the module's dependency and every test double with it.

## Consequences

- Under `working_branch = None` nothing changes: the operating branch *is* the dev branch, which is what both sites were reaching for.
- A branch abandoned by a run that ended between worktree creation and the first commit — a usage limit, a transient agent error, a credential failure, all of which `managed_worktree` re-raises without marking the worktree preservation-worthy — is deleted with its worktree instead of surviving as a stale base.
- Issue branches left behind by pycastle 0.8.2 and earlier are repaired on first reuse rather than needing operator cleanup.
- Amends ADR 0060 by completing it at the two sites that read `main` rather than the checkout.
