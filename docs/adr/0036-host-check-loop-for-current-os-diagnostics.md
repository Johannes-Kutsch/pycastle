# Host check loop for current-OS diagnostics

Add `pycastle check` as an operator-invoked host check loop: it refreshes the local branch using the same pull/update recovery behaviour as `pycastle run`, requires a clean working tree, runs `HOST_CHECKS` in a temporary local worktree on the current OS, and stops after diagnosing failures and filing issues. The default `HOST_CHECKS` run only the pytest host suite because the other default checks provide the same signal on Windows and Linux.

The command exists because Windows host-suite drift is real, but a hard Windows pre-merge gate is impractical for overnight Raspberry Pi AFK runs and GitHub Actions alone still leaves manual diagnosis work for the maintainer. Failed host check commands each spawn a host-check issue agent in the normal agent environment with minimal host context: host OS/platform, checked SHA, check name, command, and captured output. The prompt states that the failure was observed outside the agent container, applies the same AFK/HITL issue-filing policy as the preflight-issue agent, deduplicates repeated open failures, and does not implement or merge the filed issues.

## Considered Options

- **Hard-block AFK merges on Windows validation.** Rejected: most automated work runs on Linux overnight, so this would trade host-suite cleanliness for stalled AFK throughput.
- **Quarantine merged work until Windows is checked.** Rejected: the useful workflow is not a repo-level stale-confidence marker; it is removing the manual work of diagnosing host-suite failures and turning them into actionable issues.
- **Use GitHub Actions as the workflow.** Rejected as the only answer: it can detect failures, but still pushes diagnosis and issue shaping back onto the maintainer.
- **Reuse `PREFLIGHT_CHECKS` directly.** Rejected: container preflight validates the agent/container boundary, while host checks validate the current OS and toolchain. The two command sets may diverge.
- **Run implementation immediately after filing AFK-fixable issues.** Rejected: `pycastle check` is a separate loop from `pycastle run`; it diagnoses and files only, leaving implementation to the normal planner/implement/review/merge pipeline.

## Consequences

- `HOST_CHECKS` becomes a separate config surface from `PREFLIGHT_CHECKS`.
- Passing host checks are report-only; `pycastle check` does not auto-close or relabel earlier host-check issues.
- Preflight prompt behaviour remains independent; the host-check prompt stays minimal and host-boundary-specific rather than forcing a shared prompt refactor.
