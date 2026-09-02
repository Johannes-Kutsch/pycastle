# Per-phase session namespacing for improve mode

The improve agent's four phases share a service-owned conversation via the `(role, worktree_path)` UUID or provider session id, but phase 03's vertical-slice work touches none of phase 02's drafting prose. We split phase 03 onto its own session via a **session namespace** — a small string folded into both the provider session identity and the per-role session signal dir path. Phases 01/02/04 share `main`; phase 03 uses `issues` and starts fresh.

Phase 1 → phase 2 is a strict transcript handoff. Phase 2 may start only when phase 1 completed with a picked candidate and the same agent service has the same resumable `main` transcript. Phase 1 completion records service/session identity metadata so the phase 2 gate can distinguish the exact transcript from "some resumable state" or provider fallback. If that finished, resumable same-service transcript is absent at phase 2 entry, the improve attempt prints a status-only restart notice, wipes the improve sandbox, and the next iteration starts fresh from phase 1 rather than filing a PRD from partial context. Cross-service fallback at this seam restarts phase 1 fresh on the new service instead of running phase 2 on a different service. The parent PRD issue produced by phase 2 is the durable picked-candidate handoff; phase 1 does not persist a separate candidate payload.

Service/session identity metadata is not improve-only. It is useful operational state for every worktree because it explains which provider session a role actually used and gives exact-resume diagnostics. Improve phase 1 is the strict consumer of that metadata; the metadata itself is not a durable semantic handoff unless a phase contract explicitly makes it one.

Phase 03 receives the PRD's number, title, body, comments inlined via `{{ISSUE_NUMBER}}` / `{{ISSUE_TITLE}}` / `{{ISSUE_BODY}}` / `{{ISSUE_COMMENTS}}`. The PRD number crosses the namespace boundary via the existing agent-output protocol: phase 02 emits `<issue>{"number": N, "labels": []}</issue>`; `process_stream` surfaces `IssueOutput(number=N, labels=[])`; `improve_phase` captures `output.number` and drives a fresh `GithubService.get_issue` fetch when assembling phase 03's args. No `_prd_issue` persistence file — non-persistence trades a rare crash window (orphan-reset → one dead PRD on GitHub, manual cleanup) for plumbing simplicity.

## Considered Options

- **Single shared Claude conversation (status quo).** Rejected: phase 03 pays cache-warming cost on every turn against the full scan + PRD body; actively wants to re-scan against fresh `CONTEXT.md` vocabulary.
- **`force_fresh: bool` flag on `RunRequest`.** Rejected: short-circuits `decide_agent_run_kind` and still requires moving tracking files out of the role session dir.
- **Expand UUID derivation only, without partitioning the signal dir.** Rejected: `has_resumable_session(role_dir)` would still see dir non-empty (from phase 01–02) and pick Resume → continuation prompt to a session Claude has never seen → deterministic confusion. UUID and signal-dir must move together.
- **Per-phase subdir (every phase its own namespace).** Rejected: phases 01→02 benefit from shared transcript — phase 02 reuses phase 01's candidate identity and AFK-safety reasoning.
- **Per-group subdir — chosen.** Two groups: `main` (01/02/04) and `issues` (03). `RunRequest.session_namespace: str = ""` threads through UUID derivation and role-dir path. Empty default preserves byte-identical behaviour for all other roles.
- **Persist the picked candidate from phase 1 to disk.** Rejected: the intended durable handoff is the parent PRD created by phase 2. Persisting a compact candidate would let phase 2 proceed without the full scan transcript, weakening the strict phase 1 → phase 2 transcript requirement.
- **Cross-service fallback directly from phase 1 to phase 2.** Rejected: unlike Implementer → Reviewer and other worktree-backed handoffs, phase 1's useful work exists in the provider transcript rather than in committed worktree state. A different service cannot inherit that transcript, so fallback must restart phase 1.
- **Persist PRD number to disk.** Rejected: only covers a sub-second window with no I/O; orphan-reset costs nothing in normal flow.
- **Re-fetch PRD body via agent inside phase 03.** Rejected: extra agent turn to re-derive what the host already has; also fails if `gh` mis-auths in container.
- **Persist the candidate list.** Rejected here: single-candidate scan meant the list was always one entry and the durable handoff was the PRD; persisting a shortlist would let phase 2 proceed without the full scan transcript (see **Amendment** below).

## Consequences

- `RunRequest.session_namespace: str = ""`. `agent_runner.run` computes `role_session_dir = mount_path / ".pycastle-session" / role.value / namespace`. `derived_session_uuid(role, worktree_path, namespace="")` folds namespace only when non-empty — empty produces byte-identical UUIDs.
- `improve_phase` selects `namespace="issues"` for `03-issues.md`, `"main"` for every other phase. Phase 04 stays in `main` because it needs phase 01's shortlist-rejection reasoning.
- Before phase 2 starts, the selected agent service must match the service identity recorded when phase 1 completed, and that service must report the same `main` namespace session as resumable. A false Fresh decision at this point is a correctness failure, not an acceptable fallback. If service fallback chooses a different service, the current improve sandbox is wiped and the next iteration runs phase 1 first.
- Service/session identity metadata may be recorded for every worktree, not only improve-sandbox. For normal worktree-backed role handoffs it is diagnostic and resume-supporting state; for improve phase 1 → phase 2 it is part of the strict gate.
- Codex phase 1 → phase 2 recovery must be exact. A saved `thread_id` is authoritative. If the saved id is missing, recovery from rollout files is allowed only when the same namespace contains exactly one recoverable `thread.started.thread_id`; multiple candidates are ambiguous and trigger the improve-loop restart path rather than "latest rollout wins."
- `_phase_progress` / `_phase_in_flight` live at `.pycastle-session/improve/` (role-level), sibling to `main/` / `issues/`. Success-path `shutil.rmtree(role_session_dir)` wipes everything; `wipe-before-Fresh` operates only on the per-namespace path.
- `_phase_in_flight = "02-prd"` means phase 2 already started after a successful transcript handoff and should resume as a mid-phase continuation; only clean entry into phase 2 after `01-scan:picked` performs the strict handoff check.
- `IssueOutput(labels, number)` produced by IMPROVE-role parser on JSON-form `<issue>{"number": N, "labels": [...]}</issue>` alongside `<promise>COMPLETE</promise>`. Bare-integer `<issue>N</issue>` (phase 03 sub-issues) continues to be ignored.
- `_ImproveDeps` gains `gh_svc: GithubService`. `GithubService` gains `get_issue(number) -> dict`. `improve_phase` calls `get_issue` + `get_issue_comments` once per phase 03 entry.
- Phase 02 prompt drops `## Dedup check` section (no remaining caller after orphan-reset semantics). Phase 03 prompt gains `# CONTEXT` block mirroring `implement-prompt.md`.
- **Orphan-reset:** if `last_id == "02-prd"` AND `in_flight_id != "03-issues"`, in-memory PRD number is lost; unlink `_phase_progress` and restart from phase 01; orphan PRD requires manual `gh issue close`.
- The session-resume primitive is unchanged; the session namespace is additive on top of it.

> **Note (ADR 0036).** Phase 03 later moved back into the `main` namespace and now resumes the Scan/PRD transcript; the `issues` namespace is retired. ADR 0036 supersedes the phase-03-split above, but this ADR's namespace mechanism, strict phase-1→2 gate, and PRD-as-durable-handoff still stand.

> **Amendment (multiple candidates + persisted candidate list).** The single-candidate model above is superseded by multi-candidate scanning (ADR 0058). Two earlier decisions are reversed: (1) **Persisting the candidate list is now permitted.** After scan, `ImprovePhaseDriver` writes a `_candidate_list` file (ordered `ScanCandidateItem`s) and a `_candidate_cursor` file to the role session dir, letting the host resume multi-candidate processing across interruptions without repeating the scan. The strict-transcript invariant that guarded the single-candidate handoff is satisfied differently — the scan transcript is forked into per-candidate namespaces (`candidate/N`) after scan completes, so each PRD and Issues phase runs in an isolated namespace copy rather than sharing `main`. (2) **The orphan-after-02 reset is retired.** The in-memory PRD number loss that motivated the reset is eliminated: the host records the PRD number in the per-candidate `candidates/<N>/_candidate_record` file immediately after phase 02 completes, so a crash between phase 02 and phase 03 no longer means unrecoverable loss. On resume the driver reads the record and proceeds to phase 03; the orphan PRD manual-cleanup trade-off no longer applies.

> **Amendment (what the phase-1→2 gate actually asks).** The gate's intent stands: phase 2 must not write a spec
> from partial context. Its *signal* changes. The exact-transcript check compared `_service_session_metadata.json`
> against a derived provider session id, but nothing on the `AgentRunner` path ever writes that file —
> `record_successful_run` is reachable only through `pycastle.runtime.run_prompt`, which has no caller — so the
> check was unconditionally false in production and every application of the gate restarted improve from phase 1
> and discarded the session. The gate now asks whether the candidate namespace has a continuation to resume
> (`RoleSession(sandbox, IMPROVE, "candidate/<idx>").is_resumable()`), which is the signal `fork_namespace`
> actually copies and the signal a resume actually consumes. Retiring the gate outright was rejected: ADR 0058
> permits persisting the candidate list but satisfies this invariant "differently — the scan transcript is forked
> into per-candidate namespaces", so it still leans on the forked transcript, and a spec written from a one-line
> candidate title can be a spec for a different improvement than the one the scan found. Making the metadata real
> is tracked separately; see ADR 0066.

> **Amendment (a session namespace is path-valued).** This ADR introduced the namespace as "a small
> string", and every namespace it named — `main`, `issues` — was one path segment. ADR 0058's
> per-candidate fork then introduced `candidate/<idx>`, which is two. The writer side absorbed that
> without comment (`RoleSession.path` and `provider_state_relpath` both join the namespace as a
> relative path), but the reader that turns a role-session path back into `(worktree, role,
> namespace)` still took a single segment, so `candidate/0` and `candidate/1` both read back as
> `candidate`. Every candidate therefore derived the *same* session UUID, and the seed that
> stabilises that UUID was written one level too high, at the `candidate/` group directory. The
> collision is latent rather than live only because `AgentRunner` passes `session_uuid=None` and
> discards the derived id; it becomes real the moment improve is routed through the resident runtime
> (#2238).
>
> **A namespace is a relative path of one or more segments, not a single segment.** The reader takes
> every segment after the role. Flattening improve's namespaces to one segment (`candidate-0`) was
> the alternative and was rejected: the writer side is already path-valued, `candidate/N` is the
> shape ADR 0058 and the glossary already committed to, and a flat scheme fixes a reader by moving
> the on-disk layout. Path-valued also gives the reader a real inverse, which is what makes a
> round-trip assertion meaningful.
>
> Consequences. (1) The reader is **one** implementation, not two — the mechanism sits in
> `runtime_session.py` beside the forward formatters and parameterised by session root, with
> `RoleSession.from_path` as the typed pycastle-flavoured wrapper. The duplicate in
> `services/runtime_services.py` carried the identical defect, which is how one bug became two.
> (2) A namespace has a **canonical form** — no leading or trailing separator, never absolute, no
> `..`. This is not tidiness: the string is joined onto a path *and* folded into the UUID key, and
> those two disagree about a trailing separator, so `candidate/0/` and `candidate/0` name one
> directory but two session ids. (3) The reader's precondition — its input is a role-session
> directory — is carried by `RoleSession.from_path` being the named inverse of `RoleSession.path`,
> not by an active guard. A guard that rejected a trailing provider-named segment was rejected for
> putting provider names inside a layout function, which `provider_state_relpath` deliberately
> avoids. (4) `fork_namespace` keeps copying `_session_uuid_seed` into the target. A fork is a
> transcript copy, not a reset, so inheriting the source's seed lineage is correct; the ids differ
> because the namespace name is folded into the key. (5) The stray `improve/candidate/_session_uuid_seed`
> left in reused sandboxes is inert after the fix and is not cleaned up — `RoleSession(sandbox,
> IMPROVE).discard()` removes the whole `improve/` tree whenever improve resets.
