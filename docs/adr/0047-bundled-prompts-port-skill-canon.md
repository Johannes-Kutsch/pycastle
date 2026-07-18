# Bundled prompts are verbatim ports of the maintainer's skill canon

The bundled default prompts duplicate content that also lives in the maintainer's local agent skills (`~/.claude/skills`: `codebase-design`, `tdd`, `to-spec`, `to-tickets` + `_shared/SLICE-MODES.md`, `code-review`, `improve-codebase-architecture`). The skills are the canon: content is authored and edited there first, then ported into the prompts as verbatim text. One skill maps to one standards file (`codebase-design` → `shared/standards/_design.md`, `tdd` → `shared/standards/_implementation.md`); the workflow skills map to the improve/work prompt families.

## Decision

**Skills-first, then port.** A port may differ from its canon only in three delta classes:

1. **HITL automation** — steps that ask a human ("check with the user", "quiz the user", pick-from-HTML-report) become their autonomous equivalents (self-quiz, self-grilling with explicit pick questions, seams pre-agreed via the PRD instead of via conversation).
2. **Runtime adaptation** — mechanics the orchestrator owns: label placeholders instead of literal label names, AFK-only framing in improve mode, one-pass layer-count resolution (a phase agent cannot recursively re-invoke `/to-tickets`), the CONTEXT.md-issue-is-a-docs-slice rule, and no sub-agent mechanics (prompts must stay provider-agnostic).
3. **Dead-branch drops** — conditionals structurally unreachable in the pipeline (e.g. the prototype-snippet exception; no prototype stage exists), dropped per the prompt-token-economy rule: recurring prompt tokens are not spent on branches that cannot fire.

Everything else is byte-identical, including code examples (the skills' examples were translated to Python to make this possible). Re-sync is therefore a mechanical per-file diff against the canon.

## Considered options

- **Reference instead of duplicate** (prompts point at the skills). Rejected: consuming projects install pycastle from PyPI and never see the maintainer's home directory; the prompts must be self-contained.
- **Pycastle as canon, skills synced from it.** Rejected: the skills serve every project the maintainer works in, not just pycastle consumers; authoring gravity is there.
- **Semantic alignment without text identity.** Rejected: drift becomes invisible — the 2026-07 re-sync found a direct contradiction (implement-time refactor step vs the skill's refactoring-belongs-to-review rule) and ~90 duplicated architecture-vocabulary lines that text-level diffing would have caught immediately.

## Consequences

- Editing ported content in a prompt without editing the skill first is drift, not customization; consuming projects customize via local prompt overrides, never by editing the bundled defaults.
- The improve pipeline gains the canon's newer planning model: blocking edges only from genuine dependency, a "Files touched (tentative)" field as the overlap signal, and expand–contract sequencing for wide refactors; `coordination/plan.md` treats file overlap as conflict avoidance, never as a blocking edge.
- The reviewer owns refactoring (implement stops at green) and carries the two-axis review: spec fidelity (missing/partial, scope creep, implemented-but-wrong — fixed, not reported) and the 12-smell baseline.
