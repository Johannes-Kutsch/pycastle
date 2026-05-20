# Persist agent draft files inside the role session dir

Improve-mode prompts (`02-prd.md`, `03-issues.md`, `04-no-candidate-report.md`) write each issue body to a file then call `gh issue create --body-file <path>`. Until now agents chose `/tmp/sliceN.md` — container-scoped: on `UsageLimitError` the container is torn down (ADR 0008 unwinds; ADR 0005 requires fresh container; ADR 0006 designs around ephemeral containers) and the draft is destroyed. On `claude --resume`, history shows `Write(/tmp/sliceN.md, …)` succeeded, so the agent may re-issue against a path that no longer exists, or re-derive the body — non-deterministic, drafted-but-unfiled slices silently lost.

Prompt convention now writes drafts to `<worktree>/.pycastle-session/improve/drafts/`, sibling of `_phase_progress` / `_phase_in_flight` inside the **role session dir**. Preserved across container teardown by the existing broadened preservation rule, removed by **role session cleanup** on terminal success. Prompt-only fix — no orchestrator code changes.

## Considered Options

- **Heredoc inline `gh issue create --body "$(cat <<'EOF' …EOF)"`.** Rejected: large PRD bodies break shell quoting; loses the cross-turn re-read affordance and debugging artefact.
- **Per-slice promise markers + orchestrator parser.** Rejected: contract change extending the orchestrator's coupling to phase 03 mid-stream output; doesn't solve the lost-draft case (no marker for an unfiled slice).
- **Keep `/tmp`; skip container teardown on `UsageLimitError`.** Rejected: fights ADR 0005 (failover needs fresh container), ADR 0006 (resume around ephemeral containers), ADR 0008 (centralised catch unwinds to iteration boundary). Reset windows are hours.

## Consequences

- Risk-2 (dedup) explicitly not addressed; prompt-level dedup query at top of phases 03/04 still runs once per phase. Duplicate filings remain rare-but-possible.
- `drafts/` participates in role-session-dir invariants — any future refactor of the layout, preservation rule, or session cleanup must preserve it the same way as `_phase_progress` / `_phase_in_flight`.
- No new orchestrator term needed; drafts dir is purely a prompt-side convention riding on existing worktree-preservation machinery.
