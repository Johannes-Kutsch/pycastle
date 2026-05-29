# Shared `gh` issue-tracker fragment in default prompts

The default prompts instructed agents to use `gh` CLI for GitHub issue ops, with the recipe copy-pasted across five prompts: `diagnostics/preflight-issue.md`, `diagnostics/failure-report.md`, `improve/02-prd.md`, `improve/03-issues.md`, `improve/04-no-candidate-report.md`. Agent containers shipped without `gh` per ADR 0004's "no `gh` install requirement", so every agent following the prompt hit `command not found`. The 2026-05-18 slice-agent against `application-pipeline` failed after two retries and emitted `<promise>COMPLETE</promise>` with fabricated `<issue>351</issue> <issue>352</issue>` numbers; siblings improvised inconsistent `curl` fallbacks.

Fix: factor the recipe into a single shared include `shared/_issue-tracker.md` rendered via new `{{ISSUE_TRACKER}}` global placeholder, and install `gh` in both agent Dockerfiles so the placeholder is unconditionally executable.

## Considered Options

- **Inline `gh` in each prompt.** Rejected: reproduces six-way copy-paste brittleness.
- **Shared include with REST + curl recipes (no `gh` install).** Rejected: curl recipes pay markedly more tool-call tokens (auth header + repo derivation + JSON body file + jq per call); agents reach for `gh` from training-data familiarity. ADR 0004's "no `gh` install" sub-claim doesn't survive the agent-prompt context.
- **Shell-wrapper baked into image (`/usr/local/bin/issue`).** Rejected as premature: renaming the prompt token doesn't structurally prevent fabricated `<issue>N</issue>` on non-zero exit. Wrapper's stronger value (uniform error envelopes) needs the orchestrator-side verification layer, which is separate work.
- **Defer to consumer-owned `docs/agents/issue-tracker.md`.** Rejected: pycastle default prompts can't reference a path outside its release boundary.
- **Shared include with `gh` + install in both Dockerfiles — chosen.** Single fragment at `shared/_issue-tracker.md`, exposed as `{{ISSUE_TRACKER}}`. Same mechanism `shared/standards/_*.md` and `work/_shared-instructions.md` use. Installing in only one Dockerfile would make the placeholder silently container-conditional. Auth via `$GH_TOKEN`; repo from `gh`'s cwd-origin autodetect.
- **Orchestrator-side `<issue>N</issue>` verification.** Out of scope; identified as the actual structural fix. Filed as follow-up.

## Consequences

- `shared/_issue-tracker.md` canonical recipe set: create-issue, view-with-comments, list-by-search, add-label/remove-label, comment, close, link-as-sub-issue (`gh api repos/OWNER/REPO/issues/N/sub_issues --method POST --field sub_issue_id=M` — the one recipe escaping to `gh api` because sub-issues has no native `gh` verb). All auth via `$GH_TOKEN`; `OWNER/REPO` autodetected.
- `PromptRenderer`'s shared-file registry in `src/pycastle/prompts/pipeline.py` carries `shared/_issue-tracker.md → ISSUE_TRACKER`. Construction-time validation aborts loud if missing.
- Five prompts updated; each replaces `gh`-specific paragraph with `{{ISSUE_TRACKER}}`. `diagnostics/failure-report.md` gains the include.
- Both Dockerfiles install `gh` via apt — ADR 0015's pluggable services contract requires every container to honor the same prompt-level commands.
- Pycastle's own `docs/agents/issue-tracker.md` rewritten to match. Consumer copies need a one-time mirror; not automatic, not enforced.
- ADR 0018's `gh issue create --body-file` reference is once again accurate.
- ADR 0010's manual `gh issue close` for orphan PRD is a *user* step on the host — unchanged.
- ADR 0004's PAT-as-sole-credential stands; its "no `gh` install" sub-claim does not survive — agent prompts need `gh`, host code uses `urllib`.
- Hallucinated `<issue>N</issue>` emissions remain structurally possible until orchestrator-side verification lands. 2026-05-18 incident mitigated, not closed.
