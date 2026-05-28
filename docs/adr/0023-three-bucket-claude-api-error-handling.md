# Three-bucket Claude API error handling at the protocol parsing layer

`claude_service` inspects every Claude CLI streaming envelope and classifies any `is_error: true` result envelope into one of three buckets, raised through three sibling exceptions translated by `process_stream_from_events`:

- **429** â†’ existing `UsageLimitError` path (account-specific; `mark_exhausted`; sleep to reset). Unchanged.
- **`api_error_status >= 500`, or `is_error: true` with no `api_error_status`** â†’ new transient-error exception. Worktree preserved; sibling `CancellationToken` cancelled so not-yet-started parallel agents in the same iteration bail; agents already executing in `runner.work` finish; no `mark_exhausted`. Orchestrator continues to the next iteration where in-flight detection re-spawns the failed agent against the preserved worktree.
- **4xx other than 429** â†’ new hard-error exception, except for the exact subscription-access denial in ADR 0032. Worktree preserved; sibling `CancellationToken` cancelled; running siblings finish; one structured bug is filed via the existing `auto_file_issue` helper (per ADR 0022). The iteration boundary records the hard error and the orchestrator exits non-zero after the current iteration rather than entering iteration N+1.

The detector lives at the parsing layer, so the rule applies uniformly across every `AgentRole` â€” Planner, Implementer, Reviewer, Merger, PreflightIssue, Improve, DivergenceResolver, FailureReport.

Trigger was issue #831: a Reviewer hit a 529 Overloaded after the CLI's internal retries were exhausted. The CLI emitted a synthetic terminal `result` envelope (`is_error: true, api_error_status: 529, stop_reason: stop_sequence`). The pre-existing `_check_usage_limit` only matched `api_error_status == 429`, so the line was parsed as a normal `Result` event. `_CommitMessageHandler.extract_final()` legitimately returned `CommitMessageOutput(message=None)` per ADR 0007, the orchestrator composed a synthetic `Review #443 - â€¦` commit, flipped the stage-done sentinel, and the broken review was treated as complete on the next iteration via the review-skip path.

## Considered Options

- **Patch 529 only.** Rejected: leaves the same silent-success bug latent for every other 5xx, every 4xx-non-429, and every unnumbered `is_error: true` (network drop, CLI-internal error). One extra status-code branch in the same detector closes the entire class.
- **Reuse `UsageLimitError` with `reset_time=None` for 5xx, guarding `mark_exhausted` against the synthetic case.** Rejected: conflates account exhaustion with server-wide outage; future readers can't tell from the exception type which condition fired; `mark_exhausted` guard is a behaviour-leak through the type system.
- **One unified `AgentApiError` covering 5xx + 4xx-non-429, with orchestrator branching on a status field.** Rejected: 5xx and 4xx-non-429 have different orchestrator-level policies (retry-next-iteration vs halt-after-iteration; no auto-file vs auto-file). Encoding the branch in the exception hierarchy makes the iteration-boundary handler trivial; encoding it in a status field re-introduces the same exhaustive-match foot-gun ADR 0008 was written to remove.
- **Detect 5xx and cancel running siblings too (full mid-iteration unwind).** Rejected: 529 may be brief; already-running siblings on different issues are likely to complete cleanly, and cancelling them throws away work. Mirrors the existing `UsageLimitError` cancellation discipline â€” not-yet-started siblings bail via `is_cancelled`, running siblings finish.
- **On 4xx, halt mid-iteration (cancel running siblings).** Rejected: 4xx is request-specific; sibling agents on unrelated issues are not implicated and should be allowed to commit their work before the orchestrator exits.
- **On 4xx, label the failing AFK issue `needs-info` / `ready-for-human` to suppress re-pickup.** Rejected: conflates "the agent's API call failed" with "the issue itself needs human review"; loop risk on a persistent 4xx is accepted because the auto-filed bug is the operator-visible signal and human recovery (rotate token, fix prompt) gates re-running.
- **Special-case the subscription-access denial as hard error too.** Rejected in favor of ADR 0032: this exact message signals a permanent Claude account exhaustion condition, not a generic API fault.
- **Three sibling exceptions at the parsing layer, branched policy at the iteration boundary â€” chosen.**

## Consequences

- `_check_usage_limit` in `claude_service.py` is widened (or sibling-paired) so that *any* `is_error: true` result envelope is surfaced as a typed event; no `is_error: true` line is ever yielded as a `Result`.
- `ClaudeService.run` yields three event variants for the failure cases â€” existing `UsageLimit` plus two new ones â€” and `process_stream_from_events` raises three sibling exception classes.
- The exact subscription-access denial result is intercepted before the hard-error path and handled as permanent exhaustion of the active Claude account.
- `managed_worktree.__aexit__`'s broadened preservation rule recognises the two new exceptions alongside `UsageLimitError`; worktree is preserved on all three.
- `agents/runner.py`'s exception arm at the `runner.work()` call site catches the two new exceptions and cancels the shared `CancellationToken` (mirroring the existing `UsageLimitError` arm) but does **not** invoke `mark_exhausted`. Not-yet-started siblings observe the cancelled token at `RunRequest` entry and bail; running siblings are not interrupted.
- The iteration boundary in `iteration/__init__.py` gains two new top-level catches per the ADR 0008 pattern: transient â†’ next iteration with no sleep; hard â†’ record + exit non-zero after current iteration.
- On the hard path, the iteration boundary calls `auto_file_issue` (ADR 0022) with title `[pycastle] Claude API <status>: <first line>`, labels `["bug", "needs-triage"]`, body carrying the raw `result` envelope and agent-role context. Gated by `auto_file_bugs`; falls back to prefilled `issues/new?â€¦` URL when the gate is off.
- Both new paths emit a one-line `StatusDisplay.print` message at the same seam UsageLimit uses (no new UI surface); transient names the failing agent + status, hard additionally names the auto-filed issue URL.
- Stage-done sentinel never flips on either new path â€” the protocol raises before the `Result` event reaches a role handler, so role-session cleanup is skipped and `RoleSession.is_resumable()` remains true.
- Cross-role behaviour change: Planner / Merger / PreflightIssue / Improve / DivergenceResolver on a 5xx no longer raise `PromiseParseError` / `PlanParseError` / `IssueParseError` â€” they raise the transient exception, lifting them into the same preserve-and-retry semantics implementers and reviewers get. Same lift applies to the 4xx path: those roles now auto-file rather than crashing the orchestrator.
- Scope is Claude-only. `services/codex_service.py` has its own `_extract_usage_limit` parser; an audit for the same silent-success window in Codex is tracked separately, not bundled here.
- Regression test: a synthetic NDJSON stream containing the exact terminal `result` envelope from #831 is fed into `process_stream`; assertion is that the transient exception is raised, not that a `CommitMessageOutput(message=None)` is returned.
