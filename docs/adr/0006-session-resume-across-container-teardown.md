# Session resume across container teardown

> **Amended (#640).** Fail-soft fresh fallback removed. Non-typed Resume exceptions retry once, then → `FailedOutput(failure_class="non_typed_crash")` via Failure-Report path.
>
> **Amended (#692) per ADR 0015.** Claude session files moved to `.pycastle-session/<role>/[<namespace>/]claude/`. `CLAUDE_CONFIG_DIR` and `--resume`/`--session-id` are claude-private. Role-dir-empty stage-done predicate unchanged.

Interrupted agents resume via `--session-id <UUID>` (Fresh) or `--resume <UUID>` (Resume), where UUID is derived per `(role, worktree_path)`. Non-empty role session dir is the resume signal. On Resume, only the shared continuation prompt is sent. Non-typed Resume failures retry once; second failure → `FailedOutput`.

## Considered Options

- **Always restart from scratch.** Rejected: cannot preserve in-flight reasoning.
- **Replace skip with session-resume.** Rejected: skip is durable (branch ref), resume is ephemeral (worktree). Orthogonal.
- **Derived UUID per `(role, worktree_path)` — chosen.** `uuid.uuid5(uuid.uuid5(NAMESPACE_DNS, "pycastle.<role>"), str(worktree_path.resolve()))`.
- **`.git/info/exclude` idempotent append — chosen.** Local, never-committed, shared across worktrees.
- **Re-launch as Fresh on non-typed Resume crash.** Rejected (#640): common transient crashes triggered silent history wipes.

## Consequences

- `CLAUDE_CONFIG_DIR` per role; `--session-id` (Fresh) or `--resume` (Resume), managed by `ClaudeService`.
- **Prompt-shape contract:** Fresh → role prompt; Resume → continuation prompt only.
- **Broadened preservation rule:** `dirty OR usage_limit OR session_resumable`.
- `managed_worktree.__aenter__` reuse: if worktree exists, branch matches, role dir resumable → skip create.
- **Cleanup-on-success load-bearing**: `RoleSession(...).mark_done()` inside worktree context after commit/merge. `shutil.rmtree` uses `onerror` handler clearing read-only flags (Codex creates RO git pack files).
- **Non-typed Resume retry (#640):** exception on Resume not in `{UsageLimitError, AgentTimeoutError, AgentOutputProtocolError}` triggers one in-call retry. Second failure → `FailedOutput(failure_class="non_typed_crash")` → `AbortedAgentFailure`.
- Session gitignoring via `.git/info/exclude`: `.pycastle-session/` and `.claude/` appended at startup.
- Continuation prompt at `shared/resume.md`. Improve mode layers on top — subsumed by ADR 0010.
