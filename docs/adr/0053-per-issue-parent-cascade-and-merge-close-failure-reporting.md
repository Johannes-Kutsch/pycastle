# Per-issue parent cascade replaces global scan; merge close failures file operator-actionable reports

`merge_phase` previously called `close_completed_parent_issues()` after each merge batch — a method that iterated every open issue in the consuming project and queried the GitHub sub-issues API for each one. On deployments where the sub-issues API returns 401 (token scope or feature not enabled), this crashed the run after successful merges, leaving merged branches undeleted. Additionally, when `close_issue` failed for a child issue, the existing `_on_close_error` only printed a warning — a silent correctness failure because the implemented work was done but the issue stayed open with no actionable signal.

## Decision

**Parent cascade close:** `close_completed_parent_issues()` is removed from the two merge phase call sites. For each successfully-closed child issue, `close_issue_with_parents()` is called instead. The parent-lookup and sub-issue-query legs of `close_issue_with_parents` catch `GithubServiceError` and emit a warning rather than propagating, so a sub-issues API failure on any parent chain aborts only that chain and does not affect the rest of the merge batch or branch cleanup.

**Merge close failure reporting:** When `close_issue` fails for a child issue, pycastle host-files a `bug + needs-triage` issue on the consuming project's tracker — not `bug_report_repo` — following the same audience-routing principle as ADR 0026. Filing uses title-prefix dedup (`[pycastle] issue close failed`) so repeated failures across cron ticks produce one issue. The merge phase continues closing remaining issues and deletes all branches. `run_iteration` returns a new `MergeCloseFailure(filed_issue_numbers=[...])` outcome; the orchestrator prints a terminal message listing the filed issues and breaks the loop without starting a new iteration and without `sys.exit`.

## Considered options

- **Catch `GithubAuthError` in `close_completed_parent_issues` and skip.** Rejected: the real problem is the O(n_open_issues) scan. Catching the error preserves a design that queries every open issue unnecessarily. The fix should eliminate the scan, not paper over its failures.
- **Retry the sub-issues endpoint on 401.** Rejected: a 401 on the sub-issues API indicates a stable environment condition (token scope or feature availability), not a transient error. Retrying wastes time and would still fail.
- **Promote parent cascade failures to merge close failures (file + stop).** Rejected: the parent cascade is best-effort courtesy — it closes parent issues whose work is already done. A failure there does not mean an implemented issue is untracked; the child issues are already closed. Warning is the right signal.
- **Keep warning-only for child close failures.** Rejected: a merged-but-unclosed child issue has no open tracker record. The operator gets no signal, and the issue may be re-planned and worked on again. A filed report is the minimum acceptable signal.
- **File child close failures on `bug_report_repo` (upstream pycastle).** Rejected: the failure is caused by the operator's GitHub API environment (credentials, token scope), not a pycastle defect. Filing upstream creates triage noise for issues upstream maintainers cannot fix. This follows the same routing principle as ADR 0026.

## Consequences

- `close_completed_parent_issues()` is removed from `GithubService` and its tests.
- `close_issue_with_parents()` becomes the single caller-facing cascade close entrypoint. Its parent-leg error handling is non-fatal globally, so all current and future callers inherit the degraded-gracefully behavior.
- A new `file_merge_close_failure_issue` function in `bug_reporter.py` owns the consuming-project filing, dedup search, and never-raises contract. It follows the `file_operator_actionable_git_issue` shape from ADR 0026.
- `MergeCloseFailure` joins the `IterationOutcome` sum type. The orchestrator match arm prints the filed issue numbers and `break`s — no `sys.exit`, no new iteration.
- The `_on_close_error` callback in `merge_phase` is upgraded from a `status_display.print` warning to a call to `file_merge_close_failure_issue`, collecting the returned issue number.
- The pattern "*failure caused by operator's environment → consuming project's tracker, `bug + needs-triage`, title-prefix dedup, never raises*" is now applied to both git remote failures (ADR 0026) and GitHub API close failures (this ADR).
