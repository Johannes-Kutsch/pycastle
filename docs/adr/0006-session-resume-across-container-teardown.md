# Session resume across container teardown

> **Amended 2026-05-10 (#640).** Fail-soft fresh fallback removed. Non-typed exceptions on Resume now retry once against the same session, then convert to `FailedOutput(failure_class="non_typed_crash")` routing through the Failure-Report path.
>
> **Amended 2026-05-15 (#692) per ADR 0015.** Claude session files moved from `.pycastle-session/<role>/` to `.pycastle-session/<role>/[<namespace>/]claude/`. `CLAUDE_CONFIG_DIR` and `--resume`/`--session-id` are claude-private (owned by `ClaudeService` via `state_dir_relpath` and `is_resumable`). The role-dir-empty stage-done predicate is unchanged.

When an agent is interrupted mid-task the orchestrator preserves the worktree and tears down the container. Work product survives via git + implement-skip / review-skip, but *in-progress reasoning* is dropped on every interruption. Each `claude` invocation now runs with `--session-id <SESSION_UUID>` (Fresh) or `--resume <SESSION_UUID>` (Resume), where `SESSION_UUID` is derived per `(role, worktree_path)` and `CLAUDE_CONFIG_DIR` points to a per-role subdir inside the worktree. Non-empty role session dir is the resume signal. On Resume, only the shared continuation prompt is sent. On non-typed failure of `--resume`, retry once against the same session; second failure → `FailedOutput(failure_class="non_typed_crash")`.

## Considered Options

- **Always restart from scratch.** Rejected: skip preserves committed work but cannot preserve in-flight reasoning — the exact scenario the primitive targets.
- **Replace skip with session-resume.** Rejected: skip is durable across worktree removal (branch ref); resume is ephemeral with the worktree. Orphan sweep would silently demote a completed phase. Skip and resume are orthogonal.
- **Host-side volume mount for session storage.** Rejected: second cleanup path; sessions and worktrees can desync; breaks worktree-internal symmetry.
- **Random UUID per agent run, persisted in a file.** Rejected: `CLAUDE_CONFIG_DIR` already isolates per `(worktree, role)`.
- **Fixed per-role UUID constants.** Rejected: parallel agents on different worktrees sharing a constant create a silent corruption surface against any Claude-internal session-id-keyed cache.
- **Derived UUID per `(role, worktree_path)` — chosen.** `uuid.uuid5(uuid.uuid5(NAMESPACE_DNS, "pycastle.<role>"), str(worktree_path.resolve()))`.
- **Trigger resume only on `UsageLimitError`.** Rejected: filtering by cause threads cause through cancellation for no real benefit.
- **In-worktree `.pycastle-session/.gitignore` with `*`.** Rejected: doesn't hide the dir itself from `git status`.
- **Auto-append to root `.gitignore`.** Rejected: writes a tracked file, requires migration.
- **`.git/info/exclude` idempotent append — chosen.** Local, never-committed, shared across worktrees.
- **Re-launch as Fresh on any non-typed Resume crash (original fail-soft, rejected on amendment #640).** Common transient crashes were triggering silent history wipes — the exact failure the primitive should prevent. Current design retries once; second failure → `FailedOutput`. Failure-Report files a `bug` + `needs-triage` issue with a Recovery section instructing manual session wipe if transcript corruption is suspected.

## Consequences

- Every container run launches with `CLAUDE_CONFIG_DIR=/home/agent/workspace/.pycastle-session/<role>/[<namespace>/]claude/` and `--session-id` (Fresh) or `--resume` (Resume), managed by `ClaudeService`.
- `decide_agent_run_kind(role, session_dir_present) → Resume | Fresh` is pure. Skip stays inline in `run_issue` and is checked first.
- **Prompt-shape contract:** Fresh → role prompt only; Resume → continuation prompt only.
- **Worktree-preservation rule** broadens to `dirty OR usage_limit OR session_resumable`, where `session_resumable := role_dir.is_dir() AND any(role_dir.rglob("*"))`.
- `managed_worktree.__aenter__` reuse path: if worktree exists, branch matches, role dir is resumable → skip create.
- `branch_worktree` (merge-sandbox) gains the same symmetry: `teardown_worktree` and `delete_branch` skipped together when predicate holds.
- **Cleanup-on-success is load-bearing**: after every successful commit/merge, run `RoleSession(...).mark_done()` inside the worktree context — otherwise preservation rule keeps the worktree alive forever. All `shutil.rmtree` calls use an `onerror` handler that clears read-only flags before retrying, because agent CLIs (notably Codex) create read-only git pack files inside the session dir that `shutil.rmtree` cannot delete on Windows without chmod.
- **Non-typed Resume retry (#640):** any exception from `runner.work()` on Resume not in `{UsageLimitError, AgentTimeoutError, AgentOutputProtocolError, PreflightFailure}` triggers one in-call retry. Second failure → `FailedOutput(failure_class="non_typed_crash")` → `AbortedAgentFailure`.
- Session gitignoring via `<repo>/.git/info/exclude`: orchestrator startup appends `.pycastle-session/` and `.claude/` idempotently.
- Continuation prompt at `shared/resume.md` shared across roles; assumes conversation history present.
- Improve mode (#408) layers multi-prompt-within-one-run on top of this — subsumed by ADR 0010.
