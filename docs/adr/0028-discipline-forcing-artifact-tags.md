# Discipline-forcing artifact tags for behavior-slice implement and review

Every required step in an agent prompt either produces a load-bearing artifact tag — an output the agent cannot fake without doing the work — or is acknowledged as best-effort. Prose-only disciplines drift under context pressure; artifact-producing disciplines do not, because the act of producing the artifact *is* the discipline.

Two concrete applications, both motivated by #806's reported failure mode:

1. **`implement/behavior.md`** — collapse Phase A and Phase B into a uniform per-behavior loop. Require a **repeatable `<behavior>` tag**, one per behavior landed, carrying: behavior name, observable surface, test file path, and real failing-test pytest output. Host enforces ≥1 on behavior-slice. The current one-shot `<first_behavior>` / `<failing_test>` gate retires — it covered only the tracer bullet, leaving every subsequent behavior in B2 as unenforced prose, which is where the reported drift lives.

2. **`review-prompt.md`** — Reviewer's seven prose-only steps gain two artifacts: `<reviewed_diff>` (paste of `git diff main... --stat` plus a one-line summary per changed file) and `<checks_passed>` (final `FEEDBACK_COMMANDS` summary line). Step 3 (read the diff) gates every downstream step; step 1/7 check-runs prove the working state. The other five steps stay prose-only — they're either downstream of the diff read or already produce a code-level artifact (commits).

`refactor-slice` and `docs-slice` are unchanged: the diff is already the artifact, and no analogous prose-only discipline appears in their prompts. Other prompts (`plan`, `merge`, `preflight-issue`, `diverge`, `failure-report`, `improve/*`) get structural-skeleton consistency only; implementation agents auditing them must stop and open a sub-issue rather than introducing a new protocol tag unilaterally.

## Considered Options

- **Prompt-only nudges** (tighter prose, visual checklists, self-verification step before `<commit_message>`). Rejected — same mechanism that today drifts under context pressure; polishing prose does not change the failure mode.
- **Orchestrator-level per-behavior enforcement** — Phase A emits a behavior manifest, orchestrator re-spawns the Implementer once per behavior with the gate applying each turn. Rejected as out of scope for #806 (prompts only) and unjustified given that the lighter mechanism (forced artifact production) is what makes Phase A's existing gate work — host parsing only enforces presence, not authenticity.
- **Per-behavior tags, host enforces presence (≥1)** — chosen. Same trust model as today's Phase A: produce the failing-test paste, or fail the protocol. Scales to N behaviors at the cost of N pytest pastes in agent output (output tokens, not input — bounded and acceptable).
- **Token compression of prompts** as a parallel goal. Rejected — token-blind policy. Dedupe across prompts (in the spirit of ADR 0021) is the legitimate win; per-prompt compression deletes the redundancy that often *is* the discipline (Phase A's gate is restated three different ways for a reason).
- **Cross-reference paper (arxiv 2406.06608) as a standalone audit deliverable.** Rejected — paper audits rot. Paper findings are cited as evidence on demand in commit/PR messages where a non-obvious rewrite choice needs justification, not as a checklist to satisfy.

## Consequences

- `agent_output_protocol` learns a repeatable `<behavior>` parser for `IMPLEMENTER` on behavior-slice; `<first_behavior>` and `<failing_test>` parsers retire. Missing on behavior-slice raises an `AgentOutputProtocolError` subclass; refactor-slice and docs-slice runs ignore the tag.
- Reviewer output gains required `<reviewed_diff>` and `<checks_passed>` tags; missing raises the same protocol-error family. `<commit_message>` remains optional per ADR 0007.
- `implement/behavior.md` rewrites: prelude (explore + derive behavior list) → per-behavior loop (RED → emit `<behavior>` → GREEN); the behavior gate's forbidden-edits rule still fires for behavior #1 only — once the first artifact emits, the gate is satisfied and the same discipline carries through subsequent iterations via the tag requirement, not a tool-permission gate.
- `review-prompt.md` rewrites: numbered workflow unchanged in spirit, output section adds the two new tags with example shapes mirroring `<behavior>`.
- `CONTEXT.md` updates: replace `behavior gate` definition (no longer phase-bounded), retire Phase A / Phase B terms, add `<behavior>` / `<reviewed_diff>` / `<checks_passed>` entries to the Agent Output Protocol section, update `implement/behavior.md` and `Reviewer` entries.
- `_placeholder-info.md` and the placeholder parser test stay green — no new placeholders, only new output tags.
- `review-prompt.md` is not forked by slice mode in this change. If future evidence shows refactor or docs reviews need a different artifact contract, that's a separate ADR.
- The `diverge-prompt.md` negative-constraint ("do not run preflight checks") stays prose-only — absence of action cannot be artifacted cheaply, and the downstream `get_safe_sha()` preflight pass already catches violations.
- Other prompts touched in #806's scope get structural-skeleton consistency only. Any per-prompt audit that concludes "this needs a new protocol tag" must stop and open a sub-issue rather than land the tag in the #806 PR.
