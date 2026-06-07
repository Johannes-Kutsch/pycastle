# Codex auth-lineage exhaustion is operator-actionable and deduped

Superseded by ADR 0043.

Codex can fail with a refresh-token reuse error when the current OAuth lineage can no longer refresh. In the observed failure, the CLI reported that the refresh token had already been used. This is not a consuming-project defect and it is not a pycastle bug in the sense of an internal crash. It is a local operator credential problem on the machine running pycastle.

Pycastle should surface that condition to the operator without requiring log inspection by filing one issue on the consuming project's issue tracker with `bug` and `needs-triage`. The issue should be deduped across repeated runs while the credential remains broken so cron-driven runs do not spam new issues every tick.

The failure is intentionally scoped narrowly. This ADR does not claim that every Codex authentication error is operator-actionable, and it does not redefine missing-host-auth handling. It covers the exact refresh-token-reused lineage failure family only.

## Considered Options

- **Treat the failure as a consuming-project bug.** Rejected: the project code is not at fault, and the operator would be sent to the wrong tracker.
- **Treat the failure as a pycastle upstream bug.** Rejected: pycastle is not the source of the credential state that needs repair, so filing upstream would create noise the pycastle maintainers cannot resolve.
- **Treat the failure as a local credential problem and stop silently.** Rejected: the operator asked for a visible notification without log inspection, so a silent stop hides the action required to recover.
- **Treat the exact refresh-token-reused failure as an operator-actionable issue on the consuming project's tracker, with dedupe â€” chosen.**

## Consequences

- The exact Codex refresh-token-reused failure is classified as a local operator credential problem.
- Pycastle files or reuses one `bug` + `needs-triage` issue on the consuming project's tracker for that failure signature.
- Repeated cron runs while the credential remains broken converge on the same open issue instead of creating duplicates.
- The issue body should preserve the raw Codex error text so the operator can see the exact failure without opening logs.
- This policy stays narrow so unrelated Codex auth failures can still be classified separately later.
