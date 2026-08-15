# Operating the working branch through refs, not a root checkout

Status: accepted. Lands with #2128.

Pycastle no longer checks the **working branch** out in the repo root. It resolves the **safe SHA** from a ref, advances the branch with a fetch that names it on both ends of the refspec, and pushes it the same way — none of which need a checkout. The one operation that genuinely needs a working tree, merging an issue branch, moves into a sandbox worktree. The repo root belongs to the operator for the whole life of a run, and pycastle neither reads nor writes the branch checked out there.

## Why

ADR 0057 named the two branches in config, but left every phase operating on whatever was checked out in the root: it checked the working branch out at startup and depended on it staying there. Two costs followed.

**The operator could not use their own repository while a run was live.** Three clean-tree gates — startup, preflight, merge — refused to proceed while the shared checkout held any modified tracked file. An uncommitted edit intended for the **dev branch** stalled a live run; the startup gate aborted outright, and the other two printed one message and then polled every ten seconds forever, so a stalled run was indistinguishable from a hung one. Separating pycastle's work from the operator's is the entire point of having a working branch, and a shared working tree undoes it.

**The anchoring ADR 0057 promised was never enforced.** It was established once at startup and never re-checked. Because the merge target, the push target and the pull target were all read from the checkout rather than from `Config.operating_branch` — which existed and no phase read — a checkout that drifted mid-run would silently retarget the run onto another branch, including the dev branch that ADR 0057 guarantees is read-only.

ADR 0057:33 considered exactly this decision and rejected it: *"the merge/fast-forward/push path is built around operating on the checked-out root, so this would be a far larger change for no near-term benefit."* Both halves have since changed. The benefit is operator/agent concurrency, which is what the working branch exists to provide. And the change is smaller than it was, because ADR 0026 has since built the host-owned **merge-sandbox worktree** that the one tree-requiring operation needs.

## Decision detail

- **The root is never touched.** Pycastle does not read or write the repo root's checked-out branch. It is not checked out at startup, and ADR 0057's "left checked out on exit" behaviour is withdrawn — there is nothing to leave.
- **Refs, not checkouts.** The safe SHA is resolved from the operating branch's ref instead of the root's HEAD. The **preflight pull** becomes a fetch naming the operating branch on both ends of the refspec, which is fast-forward-only by nature, so ADR 0021's retry profile and the divergence-resolver escalation are preserved rather than reimplemented. The push names the branch the same way.
- **Merges go to a sandbox.** The **programmatic merge path** stops merging in the root; clean merges join conflicting ones in a sandbox worktree created at the operating branch tip, and the result fast-forwards the ref. One merge route replaces a fast path and a recovery path with different assumptions.
- **No new worktree, no new branch.** The working branch already exists; it simply stops being checked out.
- **`Config.operating_branch` becomes load-bearing.** The property already existed and was dead. It is now the value every phase names.

## The gate

The three clean-tree gates in the run path are replaced by one **operating-branch checkout gate**: if the operating branch is checked out in any worktree, pycastle prints the phase and the path holding it, then rechecks every ten seconds. Working-tree cleanliness is not consulted.

Cleanliness is the wrong condition, and this is the finding that shaped the decision. Verified against git 2.47:

- Git **refuses** to fetch into a checked-out branch even when that branch's working tree is clean. A clean checkout of the operating branch blocks pycastle exactly as completely as a dirty one, so cleanliness does not separate the blocked case from the unblocked one.
- A low-level ref update on a checked-out branch **succeeds silently**, and leaves that worktree reporting modifications its owner never made. Overriding is worse than waiting.

The blocking condition is therefore the checkout itself. This is strictly simpler than the two-step rule it replaces — one fact, not two — and it is the reason no persistent worktree is needed.

Following the repo's existing shape, the predicate is a pure decision function beside the branch-setup resolution rather than logic inside the phases, so every branch of it is testable without a git repository.

The gate **waits rather than aborting**, at startup as well as mid-run. That single choice is what makes migration free: an operator who still has the working branch checked out from the previous behaviour meets a message naming the path instead of a failed run, and switching that checkout away releases it. The two decisions are linked — restoring the abort would reintroduce a migration step.

## Considered options

- **A dedicated persistent worktree holding the operating branch.** Rejected, though it is the smaller code change — every existing call keeps working once handed a different path. It needs a new worktree lifecycle kind exempt from both the teardown rule and the startup **orphan sweep** (a worktree with no role session dir is otherwise treated as garbage, and its branch deleted), a migration step for existing installations, and a second full checkout on disk. Refs need none of that.
- **Keep the clean-tree gate but make it branch-aware** — block only when the operating branch is the one checked out. Rejected, and it is the dangerous option: because no phase reads `Config.operating_branch`, a green gate means pycastle proceeds to merge issue branches into, and push, whatever branch the operator has checked out. It converts a visible stall into silent writes to the dev branch. Any branch-aware gate requires the phases to be decoupled from the checkout first, at which point the gate is unnecessary.
- **Auto-stash the operator's changes.** Rejected: an unattended process must not mutate uncommitted human work.
- **Narrow the gate to the phases that touch the root tree.** Rejected: it shrinks the window without closing it, so the repo still cannot be trusted while a run is live.
- **Bound the wait with a timeout, or repeat the message.** Rejected: the wait stays unbounded with a single message, unchanged from the current mid-run behaviour. A run with agent work in flight should not discard it over a checkout releasable in seconds.
- **Sync dev into working automatically** so the operator's dev-branch edits reach the run. Rejected here as it was in ADR 0057: it enlarges the blast radius and pulls conflict handling onto a path that must stay boring. Keeping the working branch current with dev remains a human's job.

## Consequences

- Checking the operating branch out by hand pauses a running pycastle, clean tree or not. This is deliberate: it is pycastle's branch.
- The dev branch's read-only guarantee becomes structural rather than incidental. It held only because the right branch happened to be checked out; it now holds because no phase can name anything but `Config.operating_branch`.
- `pycastle check` is unaffected and keeps its own clean-tree check on the repo root. It is invoked by hand, against the tree the operator is looking at.
- The clean-tree predicate itself is unchanged, including its tolerance of untracked files, and stays in use for worktree teardown, the orphan sweep, and the interrupted-work clause given to a fresh dispatch on a dirty worktree.
- Delivery order is load-bearing. A state in which some phases name the operating branch while others still read the checkout is precisely the silent-write hazard above. Phases must be decoupled first, then the gate replaced, and only then the root checkout removed.
- Amends ADR 0057: its **Root checkout** clause is withdrawn, and its rejection of the refs-only option no longer holds.
