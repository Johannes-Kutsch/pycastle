# The operating-branch refresh decides from refs, not from stderr

Status: accepted. Lands with #2165.

The **preflight pull** stops being one `git fetch origin <branch>:<branch>` and becomes two steps with a decision between them: update the remote-tracking ref, compare it against the local **operating branch** ref, then act on one of four named outcomes. It is renamed **operating-branch refresh**, because it is neither a pull nor a single fetch. ADR 0060's claim that the refspec is "fast-forward-only *by nature*" is withdrawn: it is fast-forward-*enforced*, which is a different thing, and the difference killed every run whose operating branch was ahead of origin.

## Why

One root, three failures: the refspec collapses four distinct ref relations into one exit code, and the retry classifier was then left guessing at them from stderr text.

**Local ahead is not a failure, and it is the normal state.** `git fetch origin B:B` is rejected when the local branch merely *contains* origin's tip, because the update would move the ref backwards. That is the state after every `advance_branch_ref` before its push, and after an operator brings the **working branch** up to the **dev branch** by hand — which ADR 0057 and ADR 0060 both name as a human's job. Measured against git 2.47:

| State of `B` vs `origin/B` | `git fetch origin B:B` | What it should mean |
| --- | --- | --- |
| equal | exit 0 | nothing to do |
| origin ahead | exit 0, fast-forward | advance the local ref |
| **local ahead** | **exit 1, `! [rejected] … (non-fast-forward)`** | **nothing to do** |
| diverged | exit 1, `! [rejected] … (non-fast-forward)` | divergence-resolver |

The last two are indistinguishable from stderr. No classifier can separate them; only a ref comparison can.

**The divergence-resolver rung has been dead since ADR 0060.** That ADR states the escalation is "preserved rather than reimplemented". It is not: `_DIVERGENCE_OR_CONFLICT_PATTERNS` carries `git pull` vocabulary ("not possible to fast-forward", "reconcile divergent branches"), while a rejected fetch says `non-fast-forward`. The rejection therefore fell through to the unclassified-is-transient default, slept `[10s, 60s, 300s]`, and surfaced as `OperatorActionableGitError` — which is deliberately *not* a `GitCommandError`, so the preflight handler that looks for `non-fast-forward` never saw it. The preflight tests did not catch this because their fake raises a `GitCommandError` the real `GitService` cannot produce.

**The checkout gate was only checked before the operation.** The **operating-branch checkout gate** runs once, then the fetch may sleep for six minutes; an operator who checks the branch out inside that window meets `fatal: refusing to fetch into branch …` classified as a transient network blip. The same hole is wider on the write path: three of the four `advance_branch_ref` call sites fire *after* an agent run, so the gap between gate and ref write is a full Merger or Divergence-Resolver execution, and the failure discards work that was already paid for.

## Decision detail

- **Two steps, never one.** First `git fetch origin +refs/heads/<branch>:refs/remotes/origin/<branch>` — forced, so it cannot be rejected, and explicit, so it does not depend on the configured refspec's opportunistic tracking update. Then the local ref is advanced only when that is genuinely a fast-forward, with `git fetch . origin/<branch>:<branch>`.
- **Four named outcomes**, decided from two facts (does `origin/<branch>` exist, and which ancestry direction holds): `AlreadyCurrent`, `FastForwardLocalRef`, `OperatingBranchDiverged`, `NoUpstreamYet`. `AlreadyCurrent` covers both "equal" and "local ahead" — there is nothing to fetch in either case, and the **safe SHA** is read from the local ref exactly as before.
- **The decision is a pure function**, taking facts and returning a named outcome, placed beside the branch-setup resolution and following its shape. It does not belong in the **remote retry decision** module: that module classifies *stderr from a failure*, and this decides *a state before anything fails*.
- **`NoUpstreamYet` succeeds.** A **working branch** that exists locally but not on origin is not an error at preflight; the branch reaches origin on its next push. Startup is deliberately left alone.
- **The checkout is re-entered, not slept on.** `GitService` raises a typed `OperatingBranchCheckedOutError`; the caller waits at the **operating-branch checkout gate** and retries, in a loop, because the gate itself waits unbounded. This applies to the refresh *and* to `advance_branch_ref`, whose callers get the same treatment. Forcing the ref stays rejected for the reason ADR 0060 measured: a low-level update on a checked-out branch succeeds silently and leaves that worktree reporting changes its owner never made.
- **Three deterministic patterns are named** in the remote retry decision module — a fetch's `non-fast-forward` rejection, `refusing to fetch into branch`, and `couldn't find remote ref` — so none of them can be mistaken for a network blip. The module's *default* is unchanged: an unrecognised stderr is still treated as transient.
- **The Divergence-Resolver becomes a Full tool policy role** (`ToolPolicy.UNRESTRICTED`), like the Merger. Resolving textual conflicts is file mutation by definition; grouping the role with the Planner under `NO_FILE_MUTATION` blocks `Edit`, `Write` and `NotebookEdit`, and left the prompt asking for something the tool policy forbids. ADR 0039 records that grouping in a single clause, but it is a **change in meaning rather than a carry-over**: the role's previous restriction (#854) was `FlagProfile(bare=True)` — a bare CLI invocation — while the very same profile type's `disallowed_tools` field was left empty for it. The Divergence-Resolver had never been barred from writing files before ADR 0039, and the glossary recorded a third variant again (`Read,Edit,Bash`), so all three descriptions disagreed.

## Considered options

- **Force the refspec (`+B:B`) and let local always follow origin.** Rejected, and it is the dangerous option: in the "local ahead" state it rewinds exactly the merged work that is waiting to be pushed. On the day this was found it would have destroyed two commits.
- **Keep the single fetch and classify after the rejection.** Rejected: it needs the same ref comparison, but pays for it with a failed git call as the normal path and leaves the guess-from-stderr shape in place.
- **Reverse the retry classifier's default so only known-transient stderr is retried.** Rejected: that is precisely the classification ADR 0021 was written to undo, and the next unknown GitHub auth blip would tear down runs again.
- **Treat divergence on the operating branch as operator-actionable and retire the resolver.** Genuinely arguable — since ADR 0060 the branch is pycastle's alone, so divergence implies outside interference — but rejected: it removes the one remaining rung of a documented escalation ladder, and the ladder's cost is bounded.
- **Keep `NO_FILE_MUTATION` on the resolver and rewrite the prompt to resolve conflicts through Bash.** Rejected: it preserves an unexplained restriction by routing an agent through a keyhole.
- **Repair a missing upstream at startup instead of tolerating it at preflight.** Rejected in favour of tolerance; the operator, not the run, decides when a local branch becomes public.

## Consequences

- The **preflight pull** is renamed **operating-branch refresh**; the old name moves to the glossary's aliases-to-avoid column. Twice now, "pull" dragged `pull_with_merge_fallback` semantics into entries describing a fetch.
- `OperatorActionableGitError` narrows: divergence, a checked-out operating branch, and a missing upstream no longer reach it, so the consuming project's tracker stops collecting issues for states pycastle can resolve or ignore.
- The divergence-resolver becomes reachable for the first time since ADR 0060, in its new shape (sandbox built from the operating branch tip, merging `origin/<operating branch>`). Its input was already sound: git updates the remote-tracking ref even when the explicit refspec is rejected, verified against git 2.47.
- Amends **ADR 0060**: the "fast-forward-only by nature" claim and the "escalation preserved" claim are both withdrawn. The lesson is kept rather than edited away — 0060 verified its gate decision against a live git and was still wrong here, because it checked the *checked-out* case and not the *ahead* case.
- Amends **ADR 0021**: its pattern list gains three deterministic strings. Its retry profile, backoff sequence, escalation target and default-is-transient stance are untouched.
- Amends **ADR 0039**'s role/tool-policy table by one row: Divergence-Resolver moves from Restricted to Full.
