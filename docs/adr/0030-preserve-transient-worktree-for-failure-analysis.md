# Preserve transient worktree for Failure-Analysis agent

`transient_worktree` unconditionally tore down on exit, including when the yielded body raised `AgentFailedError`. The `run_iteration` handler caught the error and spawned the Failure-Analysis agent with `mount_path=err.worktree_path` — a path that no longer existed. The agent's Docker container mounted an empty directory, `pip install` failed, and the diagnostic issue was never filed (#932).

## Considered Options

- **Fall back to repo root when worktree is gone.** Rejected: the Failure-Analysis agent needs `.pycastle-session/<role>/` transcripts from the worktree to produce a useful report; mounting the repo root loses session state and logs.
- **Copy session state before teardown.** Rejected: adds complexity for a problem solved more simply by deferring teardown.
- **Preserve worktree on `AgentFailedError`, defer teardown to handler — chosen.** `transient_worktree` catches `AgentFailedError`, skips teardown, and re-raises. The centralized handler in `run_iteration` wraps the Failure-Analysis agent call in `try/finally` that calls `teardown_worktree` (guarded by `!= repo_root`).

## Consequences

- `transient_worktree` imports `AgentFailedError` and uses an `except AgentFailedError` guard to skip its `finally` teardown. All other exceptions still tear down unconditionally.
- `run_iteration`'s `AgentFailedError` handler wraps the Failure-Analysis agent call in `try/except Exception/finally`: the `except` swallows failures from the Failure-Analysis agent itself (preventing a secondary `AgentFailedError` from escaping the handler), and the `finally` cleans up the preserved worktree. A `!= repo_root` guard prevents accidental teardown when the error originates from a non-worktree path.
- `teardown_worktree` is now imported in `iteration/__init__.py`.
- No change to `managed_worktree` — it already preserves on `UsageLimitError`, `TransientAgentError`, and `HardAgentError` via the broadened preservation rule.
