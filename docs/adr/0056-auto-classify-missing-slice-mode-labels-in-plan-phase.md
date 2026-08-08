# Auto-classify missing slice-mode labels during the plan phase

A `ready-for-agent` issue missing exactly one slice-mode label (ADR 0013) previously stalled on `needs-slice-type` until a human labeled it. During the plan phase we now restructure to **preflight → relabel → plan** inside one reused plan sandbox worktree (clean checkout at the safe SHA): for each missing-slice issue whose body clears the 100-char floor, a read-only LLM **slice classifier** (`ToolPolicy.RESTRICTED` — Read + Glob) inspects the issue title/body and the safe-SHA checkout, picks the slice mode, applies the label, and readiness is re-evaluated so the issue is planned and implemented in the **same** run. `needs-slice-type` becomes a fallback, not the first response.

## Considered Options

- **`needs-slice-type` as primary (status quo, human-gated).** Rejected: wastes a human round-trip on a mechanical classification pycastle can do itself.
- **Text-only classifier + "honest" agent-runtime change to drop the workspace mount.** Rejected: `run_one_shot` already mounts a Docker workspace, so the agent-runtime change would not simplify pycastle *and* would forfeit code inspection — and behavior-vs-refactor is best judged by reading the referenced code. The optional-`invocation_dir` change is filed separately on its own merits.
- **Next-round semantics (apply label now, act next iteration).** Rejected: reintroduces a full iteration of latency; re-evaluating readiness in the same run removes the stall entirely.
- **New `AgentRole` / container role for classification.** Rejected: a three-way structured classification is lighter than any existing role. Reuse the non-resumable one-shot path and the `plan_override` stage settings instead.

## Consequences

- The plan sandbox worktree is now created whenever **classification work** exists (≥1 missing-slice issue with a valid body) *or* planning work does (≥2 well-formed candidates). The pure single-well-formed / nothing-to-classify fast path still skips the worktree and the Planner.
- The classifier runs via the existing one-shot path, inheriting stage service-rotation and timeout-retry and sharing `plan_override`; it is not a distinct `AgentRole`. The one-shot rotation path is extended to honor `ToolPolicy.RESTRICTED` (previously `ContainerRunner.work` hardcoded `FULL`).
- Classifier input is issue **title + body only** (no existing labels, comments, or parent body — AFK-ready bodies are self-sufficient by design). Deterministic tie-break: refactor-vs-behavior ambiguity → `behavior-slice`. Docs-vs-code ambiguity → an `uncertain` verdict that falls back to `needs-slice-type`, carrying a one-sentence reason as the comment.
- Only malformed-slice issues are classified, so a human-corrected label is never overwritten. Each auto-applied label posts an AI-triage audit comment, matching the existing `needs-info` / `needs-slice-type` comment pattern.
- The reused-worktree fingerprint gate keys on safe SHA + open issue ids (ADR 0050); labels do not affect it, so relabeling does not invalidate the worktree between the relabel and plan steps.
