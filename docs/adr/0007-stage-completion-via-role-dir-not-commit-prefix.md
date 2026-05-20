# Stage completion signaled by role-session-dir state, not commit-message prefix

The orchestrator decides whether Implementer/Reviewer is done by inspecting `.pycastle-session/<role>/` rather than scanning commit subjects for `RALPH: Implement -` / `RALPH: Review -`. `<commit_message>` becomes optional input to the host-side commit body — absence triggers synthetic fallback (`Implement #<n> - <title>`), not a failed run.

The trigger was issue #514: the Implementer self-committed without the prefix and emitted no `<commit_message>`, putting the orchestrator into a 14-iteration retry loop. Conflating "describe the work" (commit prefix) with "mark stage done" (resume idempotency token) had no recovery path once either side broke.

> **Amended 2026-05-15 (#692) per ADR 0015.** Role dir contents shifted from "claude session files in `<role>/`" to "one or more `<service>/` subdirs (e.g. `<role>/claude/`)". The stage-done predicate (`is_dir() AND not any(files)`) is semantically unchanged: `mark_done()` clears all children.

## Considered Options

- **Commit-prefix scan + mandatory `<commit_message>`.** Rejected: #514 is structurally unrecoverable once either convention is broken.
- **Separate `<role>.done` marker file.** Rejected: two pieces of per-stage state that must agree; creates inconsistencies.
- **Single `stages.json` per worktree.** Rejected: write contention with parallel roles; one corrupt JSON wedges both stages.
- **Drop `<commit_message>` entirely.** Rejected: agent-authored bodies aid `git log` archaeology; keeping it optional costs one `if message else default` line.
- **Role-dir presence + `has_resumable_session` content check — chosen.** Dir absent → never started (Fresh); dir present + resumable → in progress (Resume); dir present + not resumable → done (skip). Reuses the existing signal.

## Consequences

- `IMPLEMENT_COMMIT_PREFIX` / `REVIEW_COMMIT_PREFIX` constants and the `get_branch_commit_subjects` call in `iteration/implement.py` removed.
- `run_issue` skip decision reads `<wt>/.pycastle-session/implementer/` and `<wt>/.pycastle-session/reviewer/`; predicate checks recursively across `<service>/` subdirs.
- Success-path `shutil.rmtree(<wt>/.pycastle-session/<role>)` replaced by content-clearing: dir survives, contents wiped.
- `<commit_message>` optional. `process_stream` returns `CommitMessageOutput(message=None)` when absent; `CommitMessageParseError` deleted. Host commit body = agent message or synthetic `Implement #N - <title>` / `Review #N - <title>`.
- Host commit timing: "on clean agent exit" — decoupled from tag presence.
- `CommitMessageParseError` removed from the `agent_runner.py` fail-soft allowlist.
- Session-dir-presence signal is now load-bearing for three things: Resume-vs-Fresh dispatch, worktree preservation, stage-done detection. Same `has_resumable_session` predicate gates all three.
- Migration: branches with a `RALPH:` commit but no `.pycastle-session/<role>/` are treated as "not started"; re-run no-ops cleanly.
