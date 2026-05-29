# `PromptRenderer` with scoped placeholder tiers

A single `PromptRenderer` owns prompt rendering for a run. Constructed once at startup, it computes the **global placeholder pool** from `Config` and `shared/standards/` files once and caches it. At construction, validates each `PromptTemplate` variant's `{{TOKEN}}`s are members of `(global pool ∪ template.scope.placeholders)`; unknown or out-of-scope tokens abort the run before any agent starts. Each `PromptTemplate` carries a filename and a **prompt scope** (`PER_ISSUE`, `MERGE`, `PLAN`, `PREFLIGHT`, `IMPROVE_SCAN`, `IMPROVE_SESSION`, `RESUME`); each scope owns a fixed dynamic placeholder set. `render()` requires `scope_args.keys() == template.scope.placeholders` exactly — fail-loud on missing/extra/typo'd keys.

Trigger was #546: editing a prompt template required editing the test suite. The refactor moves strictness from "tests pin args contract" to "renderer ctor validates against scope registry," so prompt edits no longer touch tests.

## Considered Options

- **Per-callsite dicts + per-template tests (status quo).** Rejected: five callsites each construct their own `prompt_args`; adding a placeholder is a three-place edit.
- **Universal args dict, no scopes.** Rejected: makes no distinction between run-level constants and contextual values; misplaced `{{ISSUE_NUMBER}}` in a merge scope renders quietly.
- **Fewer scopes (collapse improve).** Rejected: `01-scan` and `02–04` have disjoint dynamic placeholder needs; `04-no-candidate-report.md` must not reference `{{TESTING_STANDARDS}}` without complaint.
- **Tight scopes, no global tier.** Rejected: forces standards/labels/checks re-declaration in every scope.
- **Lenient runtime (`scope_args ⊆ scope.placeholders`).** Rejected: exact match makes every callsite read uniformly — all required values always available.
- **Startup validation only.** Rejected: startup validates templates; runtime validates callers. Both needed.
- **Two-tier + seven scopes + `PromptTemplate` enum + ctor validation + exact-match runtime — chosen.**

## Consequences

- New `PromptTemplate` enum lists every shipped template (`filename`, `scope`); new `Scope` enum lists seven scopes (`placeholders: frozenset[str]`).
- `PromptRenderer.__init__(cfg)` reads `shared/standards/_*.md`, composes globals from `cfg`, iterates `list(PromptTemplate)`, raises on any out-of-scope token. Failure aborts `pycastle run` with offending template + token.
- `PromptRenderer.render(template, scope_args)` asserts `set(scope_args) == template.scope.placeholders`, preprocesses `` !`shell` `` expressions, merges with globals, substitutes.
- `RunRequest.prompt_file` / `prompt_args` replaced by `template: PromptTemplate` / `scope_args: dict[str, str]`.
- Five iteration callsites drop literal `prompt_args` and `prompts_dir / "<filename>.md"` constructions.
- `tests/test_default_prompts.py` deleted (370 lines); `tests/test_prompt_renderer.py` grows fixture-based machinery tests.
- Prompt edits inside an existing scope require zero test changes; validation fires at next `pycastle run` startup.
- `pycastle build` and `pycastle init` don't construct a renderer; broken template doesn't block them.
