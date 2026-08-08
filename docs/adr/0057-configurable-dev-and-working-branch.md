# Configurable dev and working branch

A run is anchored to two config-driven branches instead of the incidentally-checked-out branch. `Config.dev_branch` (default `"main"`) is a **read-only integration branch**; `Config.working_branch` (default `None`) is the branch the run actually operates on. When `working_branch` is `None` the run operates directly on the dev branch — preserving today's behavior. When set, issue branches fork from the working branch tip, completed work fast-forwards into it, and it — never dev — is what pycastle pushes.

## Why

Today pycastle silently operates on whatever branch happens to be checked out at the repo root: it pulls that branch, forks issue worktrees from its HEAD, fast-forwards completed issue branches into it, and pushes it. That couples the run's blast radius to an incidental piece of local state and gives no way to keep autonomous agent work off a protected integration branch. Naming both branches in config makes the anchor explicit and lets a run accumulate work on a side branch that a human later merges into dev via a PR.

## Decision detail

- **Dev is read-only.** Pycastle only ever *reads* dev — it seeds the working branch from it and uses it as the ancestry reference. It never merges into or pushes dev. Integrating working → dev is a human's job.
- **Working is the operating branch.** Issue branches fork from the working branch tip (so later issues in a run build on earlier merged work) and merge back into it. This is exactly the role the checked-out branch played before, now named.
- **Create-or-reuse.** If the working branch already exists it is reused as-is, with **no** reconciliation against dev even if dev has advanced — keeping the working branch current with dev is out of scope. If it is absent, it is created from `origin/<dev>` after a fetch (the true integration tip) and pushed with an upstream set (`push -u`), so the existing preflight-pull and auto-push pipeline operates on it unchanged.
- **Root checkout.** At startup pycastle checks out the resolved working branch in the repo root (creating it from dev if needed), requiring a clean working tree first, and leaves it checked out on exit — it does not restore the operator's previous branch.
- **Reviewer diff base.** The Reviewer's `git diff main...` becomes a diff against the working branch (the fork point), injected from config rather than hardcoded.
- **In-flight detection.** `has_commits_ahead_of_main` / `branch_has_commits_ahead_of_merge_base` measure against the resolved working branch instead of the literal `"main"` default, so resume detection tracks the branch issues actually forked from.

## Validation and failure modes

- Both fields are **global-forbidden** (rejected in global `config.py`, like `docker_image_name`) — they are project-specific. `dev_branch` must be non-empty.
- `working_branch == dev_branch` is a config error, rejected at load, so the read-only-dev guarantee stays unambiguous.
- A configured dev branch that does not exist on `origin` is a hard startup abort with an operator-facing error; pycastle never invents the dev branch.

## Consequences

- **Behavior change:** with `dev_branch` defaulting to `"main"` and `working_branch` unset, a default-config run now operates on `main` specifically, rather than on whatever branch happens to be checked out. A user who ran pycastle from a non-`main` checkout must now set `dev_branch` (and optionally `working_branch`) explicitly. This is deliberate — coupling behavior to the incidental checkout was fragile.

## Considered options

- **Pycastle merges working back into dev** (or keeps working continuously synced *with* dev). Rejected for the initial model: both reintroduce conflict-resolution machinery against the protected branch and enlarge the blast radius. Continuous dev → working sync can layer on later without contradicting this decision.
- **Operate purely via refs, never checking out the working branch in the root.** Rejected: the merge/fast-forward/push path is built around operating on the checked-out root, so this would be a far larger change for no near-term benefit.
- **Keep the working branch local-only** (seed from local dev, skip pull/push). Rejected: it would special-case the entire remote pipeline; seeding from `origin/<dev>` with an upstream keeps pull/push intact and yields a pushable branch ready for a PR.
- **Default `dev_branch` to the currently-checked-out branch** to preserve today's behavior when unconfigured. Rejected: the issue explicitly specifies `main` as the default, and an explicit anchor is the point of the change.
