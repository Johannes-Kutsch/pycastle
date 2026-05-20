# Per-phase session namespacing for improve mode

The improve agent's four phases share a Claude conversation via the `(role, worktree_path)` UUID, but phase 03's vertical-slice work touches none of phase 02's drafting prose. We split phase 03 onto its own Claude session via a **session namespace** — a small string folded into both `derived_session_uuid` and the per-role session signal dir path. Phases 01/02/04 share `main`; phase 03 uses `issues` and starts fresh.

Phase 03 receives the PRD's number, title, body, comments inlined via `{{ISSUE_NUMBER}}` / `{{ISSUE_TITLE}}` / `{{ISSUE_BODY}}` / `{{ISSUE_COMMENTS}}`. The PRD number crosses the namespace boundary via the existing agent-output protocol: phase 02 emits `<issue>{"number": N, "labels": []}</issue>`; `process_stream` surfaces `IssueOutput(number=N, labels=[])`; `improve_phase` captures `output.number` and drives a fresh `GithubService.get_issue` fetch when assembling phase 03's args. No `_prd_issue` persistence file — non-persistence trades a rare crash window (orphan-reset → one dead PRD on GitHub, manual cleanup) for plumbing simplicity.

## Considered Options

- **Single shared Claude conversation (status quo).** Rejected: phase 03 pays cache-warming cost on every turn against the full scan + PRD body; actively wants to re-scan against fresh `CONTEXT.md` vocabulary.
- **`force_fresh: bool` flag on `RunRequest`.** Rejected: short-circuits `decide_agent_run_kind` and still requires moving tracking files out of the role session dir.
- **Expand UUID derivation only, without partitioning the signal dir.** Rejected: `has_resumable_session(role_dir)` would still see dir non-empty (from phase 01–02) and pick Resume → continuation prompt to a session Claude has never seen → deterministic confusion. UUID and signal-dir must move together.
- **Per-phase subdir (every phase its own namespace).** Rejected: phases 01→02 benefit from shared transcript — phase 02 reuses phase 01's candidate identity and AFK-safety reasoning.
- **Per-group subdir — chosen.** Two groups: `main` (01/02/04) and `issues` (03). `RunRequest.session_namespace: str = ""` threads through UUID derivation and role-dir path. Empty default preserves byte-identical behaviour for all other roles.
- **Persist PRD number to disk.** Rejected: only covers a sub-second window with no I/O; orphan-reset costs nothing in normal flow.
- **Re-fetch PRD body via agent inside phase 03.** Rejected: extra agent turn to re-derive what the host already has; also fails if `gh` mis-auths in container.

## Consequences

- `RunRequest.session_namespace: str = ""`. `agent_runner.run` computes `role_session_dir = mount_path / ".pycastle-session" / role.value / namespace`. `derived_session_uuid(role, worktree_path, namespace="")` folds namespace only when non-empty — empty produces byte-identical UUIDs.
- `improve_phase` selects `namespace="issues"` for `03-issues.md`, `"main"` for every other phase. Phase 04 stays in `main` because it needs phase 01's shortlist-rejection reasoning.
- `_phase_progress` / `_phase_in_flight` live at `.pycastle-session/improve/` (role-level), sibling to `main/` / `issues/`. Success-path `shutil.rmtree(role_session_dir)` wipes everything; `wipe-before-Fresh` operates only on the per-namespace path.
- `IssueOutput(labels, number)` produced by IMPROVE-role parser on JSON-form `<issue>{"number": N, "labels": [...]}</issue>` alongside `<promise>COMPLETE</promise>`. Bare-integer `<issue>N</issue>` (phase 03 sub-issues) continues to be ignored.
- `_ImproveDeps` gains `gh_svc: GithubService`. `GithubService` gains `get_issue(number) -> dict`. `improve_phase` calls `get_issue` + `get_issue_comments` once per phase 03 entry.
- Phase 02 prompt drops `## Dedup check` section (no remaining caller after orphan-reset semantics). Phase 03 prompt gains `# CONTEXT` block mirroring `implement-prompt.md`.
- **Orphan-reset:** if `last_id == "02-prd"` AND `in_flight_id != "03-issues"`, in-memory PRD number is lost; unlink `_phase_progress` and restart from phase 01; orphan PRD requires manual `gh issue close`.
- ADR 0006's closing line about improve mode is superseded by this ADR; resume primitive is unchanged, namespace is additive.
