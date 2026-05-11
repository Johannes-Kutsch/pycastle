# Stage completion signaled by role-session-dir state, not commit-message prefix

The orchestrator decides whether the Implementer or Reviewer stage is already done by inspecting the worktree's `.pycastle-session/<role>/` directory rather than scanning the branch's commit subjects for `RALPH: Implement -` / `RALPH: Review -` prefixes. The agent's `<commit_message>` tag becomes optional input to the host-side commit body — its absence triggers a synthetic fallback (`Implement #<n> - <title>`), not a failed run. The trigger was issue #514, where the Implementer agent self-committed without the prefix and emitted no `<commit_message>` tag, putting the orchestrator into a 14-iteration retry loop because the prefix scan kept reporting "implementation not done" while the agent kept seeing its own prior commit and producing only a status summary. Conflating "describe the work" (the commit prefix) with "mark the stage done" (the resume idempotency token) had no recovery path once the agent broke either side of the convention.

## Considered Options

- **Status quo (commit-prefix scan + mandatory `<commit_message>` tag).** Rejected: the failure mode in #514 is structurally unrecoverable. Once the agent commits without the prefix, no further run can restore it without the agent realising the convention exists; once the agent fails to emit `<commit_message>`, the orchestrator treats the run as failed even when the work is on the branch.
- **Add a separate `<role>.done` marker file alongside `<role>/`.** Rejected: introduces a second piece of per-stage state that must agree with the in-progress dir; creates "marker present but no session content" / "session content but no marker" inconsistencies.
- **Single `stages.json` per worktree.** Rejected: write contention if roles ever run in parallel within a single issue; one corrupt JSON file wedges both stages.
- **Drop the `<commit_message>` tag entirely.** Rejected: agent-authored commit bodies are useful for `git log` archaeology; keeping the tag as *optional* costs one `if message else default` line over making it mandatory.
- **Role-dir presence + `has_resumable_session` content check — the chosen design.** Dir absent → never started (Fresh); dir present and `has_resumable_session` true → in progress (Resume); dir present and `has_resumable_session` false → done (skip). Reuses the existing signal for Resume-vs-Fresh dispatch and worktree preservation.

## Consequences

- `IMPLEMENT_COMMIT_PREFIX` / `REVIEW_COMMIT_PREFIX` constants and the `get_branch_commit_subjects` call in `iteration/implement.py` are removed. The branch's git history no longer carries an orchestrator-readable stage marker.
- The skip decision in `run_issue` reads `<wt>/.pycastle-session/implementer/` and `<wt>/.pycastle-session/reviewer/`. `implement_done := implementer_dir.is_dir() and not has_resumable_session(implementer_dir)`; `review_done` likewise.
- The success-path `shutil.rmtree(<wt>/.pycastle-session/<role>)` calls are replaced by content-clearing: the dir survives but its contents (session JSONL files) are wiped so `has_resumable_session` returns false and the next iteration sees "stage done."
- `<commit_message>` becomes optional. `process_stream` returns `CommitMessageOutput(message=None)` when absent — `CommitMessageParseError` is deleted. The host commit body is the agent message if set, otherwise a synthetic `f"Implement #{issue_number} - {issue_title}"` / `f"Review #{issue_number} - {issue_title}"`.
- Host-side commit timing is "on clean agent exit" — decoupled from tag-presence check.
- The `CommitMessageParseError` entry in the fail-soft allowlist (`agent_runner.py`) is removed.
- The session-dir-presence signal is now load-bearing for three things: Resume-vs-Fresh dispatch, worktree preservation, and stage-done detection. The same `has_resumable_session` predicate gates all three.
- One-shot migration cost: branches with a `RALPH:` commit but no `.pycastle-session/<role>/` dir will be treated as "stage not yet started." Implementer or Reviewer re-running on already-completed work no-ops cleanly.
