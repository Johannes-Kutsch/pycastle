# `PromptRenderer` with scoped placeholder tiers

A single `PromptRenderer` owns prompt rendering for a run. It is constructed once at startup, computes the **global placeholder pool** from `Config` and `coding-standards/` files once and caches it, and at construction validates that each `PromptTemplate` variant's `{{TOKEN}}`s are members of `(global pool ∪ template.scope.placeholders)` — an unknown or out-of-scope token aborts the run before any agent starts. Each `PromptTemplate` variant carries a filename and a **prompt scope** (one of `PER_ISSUE`, `MERGE`, `PLAN`, `PREFLIGHT`, `IMPROVE_SCAN`, `IMPROVE_SESSION`, `RESUME`); each scope owns a fixed dynamic placeholder set. `render()` requires `scope_args.keys() == template.scope.placeholders` exactly and fails loud on missing, extra, or typo'd keys.

The trigger was issue #546: editing a prompt template required editing the test suite. `tests/test_default_prompts.py` pinned every shipped template's placeholder set; adding `{{NEW_THING}}` forced a coordinated edit across the prompt, the callsite's `prompt_args` literal, and the test file. The refactor moves strictness from "tests pin the shipped template's args contract" to "the renderer ctor validates shipped templates against a code-side scope registry," so prompt edits no longer touch tests at all.

## Considered Options

- **Status quo (per-callsite dicts + per-template tests).** Rejected: five callsites each construct their own `prompt_args` literal; adding a placeholder is a three-place edit.
- **Universal args dict, no scopes.** Rejected: makes no distinction between run-level constants and contextual values. Templates accidentally referencing `{{ISSUE_NUMBER}}` in a merge scope would render quietly.
- **Scope-tagged templates, but fewer scopes (collapse improve).** Rejected: `01-scan` and `02–04` have disjoint dynamic placeholder needs. `04-no-candidate-report.md` must not reference `{{TESTING_STANDARDS}}` without complaint. The seven-scope split mirrors the existing boundary in `improve.py:113-117`.
- **Tight scopes, no global tier.** Rejected: forces standards/labels/checks to be re-declared in every scope that needs them. The "constants vs context" axis is real.
- **Lenient runtime: `scope_args.keys() ⊆ scope.placeholders` (subset, not equal).** Rejected: a typo'd `ISSUE_NUMER` would still fail, but exact match makes every `PER_ISSUE` callsite read uniformly — all five values are always available.
- **Drop runtime checks; rely on startup validation alone.** Rejected: startup validates templates against scope sets; runtime validates callers against scope sets. Both are needed — different bugs.
- **Two-tier placeholders + seven scopes + `PromptTemplate` enum + construction-time validation + exact-match runtime + delete `test_default_prompts.py`.** The chosen design.

## Consequences

- A new `PromptTemplate` enum lists every shipped template; each variant carries `filename: str` and `scope: Scope`. A new `Scope` enum lists the seven scopes; each variant carries `placeholders: frozenset[str]`.
- `PromptRenderer.__init__(cfg)` reads `coding-standards/*.md` files, composes globals from `cfg`, then iterates `list(PromptTemplate)` and raises on any token outside `(global ∪ template.scope.placeholders)`. Construction is the validator; failure aborts `pycastle run` with a message naming the offending template and token.
- `PromptRenderer.render(template, scope_args)` asserts `set(scope_args) == template.scope.placeholders`, preprocesses `` !`shell` `` expressions, merges scope_args with cached globals, substitutes `{{placeholders}}`.
- `RunRequest`'s `prompt_file` and `prompt_args` fields are replaced by `template: PromptTemplate` and `scope_args: dict[str, str]`.
- The five iteration callsites drop their `prompt_args` literals and `prompts_dir / "<filename>.md"` constructions; each becomes `RunRequest(template=PromptTemplate.X, scope_args={...})`.
- `tests/test_default_prompts.py` is deleted (370 lines). `tests/test_prompt_renderer.py` grows fixture-based machinery tests against synthetic templates in `tmp_path`.
- Prompt edits inside an existing scope require zero test changes. Validation fires at the next `pycastle run` startup.
- `pycastle build` and `pycastle init` do not construct a renderer; a broken template will not block them.
