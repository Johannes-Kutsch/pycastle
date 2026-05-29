# Prompt family layout

Bundled prompts are organized by prompt family instead of a mostly flat prompts directory. Direct role prompts use short domain names inside their family:

- `coordination/plan.md`, `coordination/diverge.md`, and `coordination/merge.md`
- `work/behavior.md`, `work/refactor.md`, `work/docs.md`, and `work/review.md`
- `diagnostics/preflight-issue.md`, `diagnostics/host-check-issue.md`, and `diagnostics/failure-report.md`
- `improve/01-scan.md`, `improve/02-prd.md`, `improve/03-issues.md`, and `improve/04-no-candidate-report.md`
- `shared/resume.md`

Fragments live beside the family that owns them unless reused across families. Cross-family fragments and prompt reference cards live under `shared/`, with underscore-prefixed filenames because they are not dispatched role prompts:

- `work/_shared-instructions.md`
- `shared/_issue-tracker.md`
- `shared/_placeholder-info.md`
- `shared/standards/_design.md`
- `shared/standards/_implementation.md`
- `shared/standards/_output-rules.md`

The old flat paths are not kept as compatibility aliases. Local prompt overrides must move to the new relative paths. Unknown files under the fixed local `pycastle/prompts/` override directory are rejected at prompt-renderer startup so stale old-path overrides, typos, and unused prompt files fail loudly instead of being silently ignored. This strict unknown-file rule is scoped to the fixed local override layer; arbitrary prompt roots used as complete prompt sources are not treated as local override layers. The old `{{IMPLEMENT_REVIEW_SHARED_FRAMING}}` placeholder is replaced by `{{WORK_SHARED_INSTRUCTIONS}}`; old placeholder names do not remain valid aliases.

The alternative was to keep compatibility aliases for the old prompt paths and placeholder names. That would reduce upgrade friction for existing local overrides, but would leave two public prompt vocabularies in circulation and make cross-file references harder to follow. Another alternative was to keep ignoring unknown local prompt files, matching the old loose override-layer behavior. That would preserve scratch-file convenience but make stale overrides and typos invisible. The breaking cleanup is accepted because prompt overrides are explicitly user-owned forks, and a stale override should fail loudly rather than silently shadow a reorganized default or sit unused.
