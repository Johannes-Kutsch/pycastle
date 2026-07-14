# Improve slice phase resumes the PRD session

Status: accepted (supersedes the phase-03-split decision in ADR 0008; ADR 0008's namespace *mechanism*, strict phase-1→2 gate, and PRD-as-durable-handoff all stand)

ADR 0008 ran improve phase 03 (Slice) in its own `issues` namespace, fresh, with only the PRD issue inlined. In practice the Slice Agent re-discovered what the Scan/PRD agent already knew — re-opening the same modules, re-reading the same interfaces, re-deriving the same understanding — because a fresh start carries only the PRD's *conclusions*, never the PRD agent's *working memory*. We move phase 03 into the `main` namespace so it resumes the Scan/PRD transcript. `issues` namespace retired; improve runs entirely in `main`.

## Why resume, not a richer PRD

A richer PRD body still only ships conclusions. The exploration state — which files were opened, how each interface was read, which dead-ends were ruled out — lives only in the transcript. Resuming `main` is the sole mechanism that hands the slicer that state intact. The PRD agent's reasoning the slicer most wants (rejected shortlist + why, AFK-safety self-grilling) is deliberately kept *out* of the PRD body, so it cannot reach the slicer any other way.

## Robustness: graceful degrade, not strict gate

Phase 03 entry is a *soft* check, unlike the strict phase-1→2 gate:

- Same-service, resumable `main` transcript present → **resume** (the win).
- No resumable same-service `main` transcript — crash, container teardown, or cross-service fallback (e.g. Claude exhausted, Codex takes over) → **fresh + PRD inlined**, i.e. today's behaviour. No phase-01 restart.

Phase 1→2 is strict because phase 1 has no durable artifact — its only output is the transcript. Phase 3 is not, because phase 2 already filed the PRD on GitHub; the PRD remains the durable floor, resume is a best-effort enhancement on top. Throwing away a filed PRD to restart phase 01 just because the slicer couldn't resume would be pure waste.

## `issues` namespace was never doing cross-service isolation

The Resume-vs-Fresh decision is keyed per *service*, not per namespace: the provider-state dir folds the service name in as its own path segment (`.pycastle-session/<role>/[<namespace>/]<provider>/`). Within one `main` namespace, Claude's transcript lives at `main/claude/`, Codex's at `main/codex/`; the run-kind decision inspects only the dispatched service's dir. So when phase 03 runs on a different service than phase 02, it inspects an empty `main/<newservice>/` and goes Fresh automatically. The graceful-degrade behaviour above falls out of the existing per-service keying — the cross-service path is byte-identical whether phase 03 lives in `issues/<svc>/` or `main/<svc>/`. The only behavioural change versus ADR 0008 is the same-service case flipping from forced-fresh to resume.

## PRD re-inlining stays

Phase 03 keeps re-fetching the PRD (`get_issue` + `get_issue_comments`) and inlining `{{ISSUE_*}}` on resume as well as fresh. The transcript holds the *original* PRD the agent wrote; the fetched copy reflects any human edit since. Re-inlining preserves ADR 0008's human-edit-honoring guarantee, feeds the degrade-to-fresh path, and keeps one prompt shape instead of two.

## Consequences

- The cache-cost trade ADR 0008 flagged is accepted: resuming `main` drags phase 01's broad scan (mostly about rejected candidates) into the slicer's context. For idle-fill improve mode the rediscovery saving outweighs it.
- PRD-number handoff and orphan-after-02 reset are unchanged in mechanism — the PRD number is still in-memory host state — but no longer cross a namespace boundary.
- `main` session UUID and `.pycastle-session/improve/main/…` paths are left untouched; retiring `issues` does not re-pave the surviving phases' identity.
