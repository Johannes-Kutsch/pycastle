# Persist agent draft files inside the role session dir

Improve-mode prompts (`02-prd.md`, `03-issues.md`, `04-no-candidate-report.md`) write each issue body to a file then call `gh issue create --body-file <path>`. Until now agents chose `/tmp/sliceN.md` — container-scoped: on `UsageLimitError` the container is torn down (the ADR 0006 centralized catch unwinds to the iteration boundary; credential failover and session resume both assume a fresh, ephemeral container) and the draft is destroyed. On `claude --resume`, history shows `Write(/tmp/sliceN.md, …)` succeeded, so the agent may re-issue against a path that no longer exists, or re-derive the body — non-deterministic, drafted-but-unfiled slices silently lost.

Prompt convention now writes drafts to `<worktree>/.pycastle-session/improve/_drafts/`, sibling of `_phase_progress` / `_phase_in_flight` inside the **role session dir**. Preserved across container teardown by the existing broadened preservation rule, removed by **role session cleanup** on terminal success. Prompt-only fix — no orchestrator code changes.

## Considered Options

- **Heredoc inline `gh issue create --body "$(cat <<'EOF' …EOF)"`.** Rejected: large PRD bodies break shell quoting; loses the cross-turn re-read affordance and debugging artefact.
- **Per-slice promise markers + orchestrator parser.** Rejected: contract change extending the orchestrator's coupling to phase 03 mid-stream output; doesn't solve the lost-draft case (no marker for an unfiled slice).
- **Keep `/tmp`; skip container teardown on `UsageLimitError`.** Rejected: fights credential failover (needs a fresh container), session resume (designed around ephemeral containers), and the ADR 0006 centralised catch (unwinds to the iteration boundary). Reset windows are hours.

## Consequences

- Risk-2 (dedup) explicitly not addressed; prompt-level dedup query at top of phases 03/04 still runs once per phase. Duplicate filings remain rare-but-possible.
- `_drafts/` participates in role-session-dir invariants — any future refactor of the layout, preservation rule, or session cleanup must preserve it the same way as `_phase_progress` / `_phase_in_flight`.
- No new orchestrator term needed; drafts dir is purely a prompt-side convention riding on existing worktree-preservation machinery.

> **Amendment (host-owned filing and multiple candidates).** The filing model changed in two stages. First, issue filing was handed from the agents to the host: agents write drafts to `_drafts/` as before, but the host reads and files them via `file_draft_set` instead of the agent calling `gh issue create`. Second, the improve scan was extended to return multiple candidates; the host processes each candidate in turn, writes `02-prd.md`'s spec draft to `_drafts/spec.md`, runs the Slice Agent which reads it and writes slice drafts, then calls `file_draft_set` and clears `_drafts/` before moving to the next candidate. The `_drafts/` directory therefore lives at role level (`.pycastle-session/improve/_drafts/`), not inside the per-candidate namespace directory (`candidate/N/`), for two reasons: (a) the host reads from a fixed role-level path and threading the active namespace into the reader would add coupling with no benefit; (b) `_drafts/` is ephemeral per-candidate — written, filed, and cleared before the next candidate begins — so sharing the path adds no ambiguity. The durable per-candidate record is at `candidates/<N>/_candidate_record` (see ADR 0058).
