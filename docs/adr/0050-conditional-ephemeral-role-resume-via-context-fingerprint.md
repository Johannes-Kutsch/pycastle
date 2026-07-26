# Conditional ephemeral role resume via context fingerprint

Ephemeral roles — Planner, Divergence-Resolver, Merger, and Improve — always discard their provider session state on clean exit. Interrupted sessions (usage limit, timeout, Ctrl-C) cannot resume on the next run; the agent restarts from scratch even when the external context that shaped the session has not changed. Improve already sidesteps this for its multi-phase pipeline via `ImprovePhaseDriver`; no other ephemeral role has an equivalent mechanism. Additionally, Improve previously had no guard against a changed safe SHA: a moved HEAD left the improve-sandbox at a stale codebase while `ImprovePhaseDriver` continued from prior phase progress.

## Decision

**Context fingerprint per role.** Before starting or resuming a session, each ephemeral role computes a hash of its canonical external inputs. The hash is stored at `<role_session.path>/_fingerprint` — a new sibling of `_continuation` and `_done`. The gate is checked before entering the managed_worktree context (the sandbox path is deterministic from `reusable_sandbox_worktree_identity()`). If the stored fingerprint matches the freshly computed one and `_continuation` exists, the session resumes. On mismatch, `role_session.discard()` is called; the session dir is gone, `_cleanup_stale_named_worktree` can then remove the worktree, and `_create_worktree` rebuilds it at the current safe SHA — no new lifecycle code required. The new fingerprint is written inside the context before the agent starts. The success path is unchanged: `discard()` on clean completion wipes everything including `_fingerprint`.

Per-role fingerprints:
- **Planner**: `hash(safe_sha + sorted_all_open_issue_ids)` — explores the codebase and plans across the full dependency graph; SHA change or any issue set change invalidates.
- **Divergence-Resolver**: `hash(safe_sha + diverging_branch)` — context-specific to one conflict at one baseline.
- **Merger**: `hash(safe_sha + conflict_branch)` — same reasoning.
- **Improve**: `hash(safe_sha)` — scans the codebase at a specific HEAD; a moved HEAD means a stale scan.

**Planner lifecycle change: `SandboxWorktreeIntent.PLAN` via `reusable_sandbox_worktree`.** The Planner's plan-sandbox was a `detached_transient_worktree` (no branch, always torn down). Switching to `reusable_sandbox_worktree` with a named branch (`pycastle/plan-sandbox`) lets `_continuation` and `_fingerprint` survive process restarts. The `NO_FILE_MUTATION` tool policy is unchanged.

**Merger: lifecycle-choice gate.** The Merger's `replaceable_merge_sandbox_worktree` unconditionally recreates the sandbox to prevent stale conflict state (ADR 0026). The fingerprint gate supersedes this precisely: fingerprint match AND `_continuation` present → same baseline, safe to resume → use `reusable_sandbox_worktree` instead. All other cases → `replaceable_merge_sandbox_worktree` as before, preserving ADR 0026's intent byte-for-byte.

**No new resume prompts.** A fingerprint match guarantees external inputs are identical to session start, so `shared/resume.md` is sufficient for all roles. Improve's `ImprovePhaseDriver` already handles context re-injection at phase transitions.

**`SandboxWorktreeIntent` replaces `ReusableSandboxWorktreeIntent`; `DetachedTransientWorktreeIntent` retired.** After the Planner lifecycle change, `ReusableSandboxWorktreeIntent` is the sole enum for named sandbox worktrees with no parallel "non-reusable" enum to distinguish from. Renamed to `SandboxWorktreeIntent`. `DetachedTransientWorktreeIntent` had two entries: `PLAN` (moved to `SandboxWorktreeIntent`) and `PREFLIGHT` (Preflight-Issue agent sandbox, excluded from this feature). `PREFLIGHT` is dropped from the enum; its single call site passes the raw string `"preflight-sandbox"` to `detached_transient_worktree` directly.

**Preflight-Issue excluded.** Short-running, rarely interrupted, and carries a partial-completion hazard: if interrupted after filing an issue but before outputting `<issue>NUMBER</issue>`, a resumed session risks re-filing. The ROI does not justify the complexity.

## Considered options

- **Include Preflight-Issue.** Rejected: short session, rare interruption, partial-completion hazard (agent may have already filed the issue before being interrupted). Deferred to a follow-up if data shows it matters.
- **Store fingerprint outside the worktree.** Rejected: the sandbox path is deterministic before context entry; `role_session.read_fingerprint()` reads the known path directly, avoiding a second storage location with its own cleanup rules.
- **Re-inject context on resume via new role-specific prompts.** Considered for Planner (fresh issue list) and Preflight-Issue (fresh check output). Rejected: fingerprint match guarantees context is unchanged — nothing to inject. Preflight-Issue was excluded, removing the only case where a scope input (check output) could drift while the fingerprint still matched.
- **Planner fingerprint without safe SHA.** Rejected: the Planner explores the codebase; a changed SHA means changed code that may alter feasibility or dependency reasoning, even when the issue set is identical.
- **Include all-open issue IDs in Planner fingerprint but only ready-for-agent IDs as the gate.** Rejected: a cleared HITL blocker lives in the all-open set but not the ready-for-agent set; omitting it would resume a Planner that was reasoning about a world where that blocker was still open.

## Consequences

- `RoleSession` gains `_fingerprint` file path, `read_fingerprint() → str | None`, and `write_fingerprint(hash: str)`; `discard()` and `start_fresh()` remove it as part of full session-dir deletion.
- New shared `prepare_fingerprint_gate(role_session, fingerprint: str)` helper — compares stored vs. current fingerprint, calls `discard()` on mismatch; called by each phase before entering the sandbox worktree context.
- `ReusableSandboxWorktreeIntent` renamed to `SandboxWorktreeIntent` with three members: `IMPROVE`, `DIVERGENCE`, `PLAN`.
- `DetachedTransientWorktreeIntent` retired; `detached_transient_worktree` receives the raw string `"preflight-sandbox"` directly at its remaining call site.
- Planner plan-sandbox switches from detached transient to named-branch reusable (`pycastle/plan-sandbox`); the orphan sweep's existing role-dir-present guard keeps it alive while session state exists and tears it down otherwise.
- Merger call site gains a pre-context lifecycle-choice: `reusable_sandbox_worktree` when fingerprint and continuation both match; `replaceable_merge_sandbox_worktree` otherwise.
- Improve gains an outer SHA guard; previously a SHA change left the improve-sandbox stale; the fingerprint gate now discards and lets the lifecycle recreate at the new SHA.
- CONTEXT.md updated: `plan-sandbox worktree`, `reusable sandbox lifecycle`, `detached transient worktree lifecycle`, `RoleSession`; new terms added: `conditional ephemeral resume`, `context fingerprint`, `fingerprint gate`, `_fingerprint file`, `SandboxWorktreeIntent`.
