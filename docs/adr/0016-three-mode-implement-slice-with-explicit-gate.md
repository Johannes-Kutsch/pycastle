# Three-mode implement slice with explicit gate

Today's single `implement-prompt.md` mixes a long standards block between §2 Behaviors and §3 Tracer Bullet, and offers no forcing function between Explore and Implement. Under context pressure the agent skips §3 — observed verbatim in the wild as `"Now I have all the context I need. Let me implement the changes:"` followed by a numbered plan with "Add tests" last. The failure has three layered causes: (a) the `to-issues` skill writes acceptance criteria *as tests* ("a test asserts X"); (b) issues mix refactor-prep with behavior steps and the prompt has no story for refactor-shaped work; (c) no checkpoint between "I understand" and "I'm writing code" forces re-entry into TDD.

Fix all three: split slice authorship into three mutually-exclusive modes (`refactor-slice`, `behavior-slice`, `docs-slice`) marked by GitHub labels, dispatch to one of three implement prompts per session, and gate the behavior prompt with a two-phase structure terminated by mandatory `<first_behavior>` and `<failing_test>` tags. **Slicing rule:** any step not verifiable by a new test of observable behavior goes into a `refactor-slice`, never inside a `behavior-slice`. Multiple refactor steps may combine into one `refactor-slice`; the dependent behavior slice lists it in `Blocked by`. Refactor slices land first.

## Considered Options

- **Fix only `to-issues` (rephrase criteria).** Rejected as sole change: prompt's structural problems persist regardless of issue quality.
- **Fix only the prompt (gate, leave issues alone).** Rejected as sole change: a gate cannot rescue an issue bundling refactor-prep with behavior — refactor steps have no "first failing test" to emit.
- **One prompt with conditional sections.** Rejected: keeps bloat that caused §3 to be skipped; introduces template-branching the pipeline lacks.
- **Mode in body as `## Mode` section.** Rejected: duplicates label data; implement agent has to body-parse to dispatch.
- **Title prefix `[refactor] …`.** Rejected: drifts under hand-editing; not queryable.
- **Two labels (`mode:refactor` / `mode:behavior`, docs unlabeled).** Rejected: silent default reproduces drift risk per `feedback_github_issue_labels.md`.
- **One label `mode:refactor`; absence implies behavior.** Rejected: same silent-default anti-pattern.
- **`mode:` namespace prefix.** Rejected: introduces a namespace convention pycastle's other labels don't use.
- **Author-judgement slicing rule (unstated).** Rejected: this is effectively today's behaviour and produces bundled slices like #205.
- **Symbol-move-only refactor rule.** Rejected as too narrow: protocol introduction, import rewires, shared-module extraction are legitimate refactor with no behavior change.
- **Touched-file-count rule.** Rejected: file count doesn't track the underlying distinction.
- **Allow small refactor along inside a behavior slice.** Rejected: session mode must be unambiguous or dispatch handles mid-session switching — exactly the state machine that erodes under context pressure. Bundle multiple refactor steps into one slice instead.
- **Refactor + docs share a stripped prompt.** Rejected: disciplines overlap but artifacts diverge; hedged language reintroduces ambiguity.
- **Reuse `AgentRole` as dispatch axis (three implementer roles).** Rejected: `AgentRole` drives stage tracking, log filenames, status display, worktree dirs — slice mode is per-issue, not per-stage.
- **Renderer-side dispatch (PromptRenderer reads labels).** Rejected: couples `PromptRenderer` to GitHub-issue shape. Explicit dispatch at the call site is grep-able and independently testable.
- **No gate; trust prompt's TDD framing.** Rejected: status quo, demonstrably insufficient. Only a mandatory artifact survives the fluency.
- **Three-line checklist gate.** Rejected: checklists get filled performatively without running the test.
- **Required tag without phase structure.** Rejected: tag becomes a ritual emitted then ignored. Phase structure reframes the whole prompt.
- **Refactor green-before/green-after gate.** Rejected: pipeline already pins to preflight-verified SHA (ADR 0014); re-running the suite at session start is duplicate work.
- **Refactor characterization-test gate.** Rejected: inverts "refactor produces no new tests." Uncovered behavior surfaced by refactor is filed as a follow-up `behavior-slice`.
- **Docs slices via pycastle-internal command, bypass agent.** Rejected: bypassing review for docs is the wrong incentive — domain-doc edits are where review earns its keep.
- **Default the slice-mode label.** Rejected: silent defaults reintroduce drift; picker fails fast on missing/unknown/multiple.

## Consequences

- **Three GitHub labels, mandatory per code-or-docs issue.** `refactor-slice`, `behavior-slice`, `docs-slice` — exactly one per issue producing code or docs. HITL architectural slices stay unlabeled on this axis.
- **Labels are first-class config and provisioned by `pycastle labels`.** `Config` gains `refactor_slice_label`, `behavior_slice_label`, `docs_slice_label`. Canonical label set grows from six to nine.
- **Three new global placeholders:** `REFACTOR_SLICE_LABEL`, `BEHAVIOR_SLICE_LABEL`, `DOCS_SLICE_LABEL`.
- **Three implement prompts:** `work/behavior.md` (two-phase TDD with gate), `work/refactor.md` (short — change named symbols, no new tests, feedback commands at end), `work/docs.md` (shortest — apply markdown edits, no code, no tests). `PromptTemplate` gains `IMPLEMENT_BEHAVIOR`, `IMPLEMENT_REFACTOR`, `IMPLEMENT_DOCS`; singular `IMPLEMENT` removed.
- **Behavior gate.** Phase A: explore (issue-scoped), name first behavior, write one failing test, run feedback. Phase A terminates with mandatory `<first_behavior>` (name + observable surface) and `<failing_test>` (real pytest output, red). No `Edit`/`Write` on non-test files before both tags. Phase B: implement to green, incremental loop, refactor, finish.
- **Refactor gate.** No new tests. Existing tests must pass. Uncovered behavior surfaced is filed as follow-up `behavior-slice`, never patched in-session. Private-helper testing forbidden per `feedback_no_private_method_tests.md`.
- **Docs gate.** No code files touched. No new tests. Output is markdown edits plus `<commit_message>`.
- **Dispatch site.** `pick_implement_template(issue, cfg) -> PromptTemplate` next to call site in `src/pycastle/iteration/implement.py`; reads `issue["labels"]`, expects exactly one slice-mode label, fails fast on missing/unknown/multiple.
- **`to-issues` slicing rule** stated verbatim in `SKILL.md` and `improve/03-issues.md`.
- **Acceptance-criteria shape per mode.** `behavior-slice`: behavior + observable-surface phrasing. `refactor-slice`: outcome-shaped. `docs-slice`: file-state-shaped. **Banlist:** "a test asserts X", "test verifies X", "unit test X", "the test should X".
- **Lockstep surfaces:** `SKILL.md`, `improve/03-issues.md`, `CONTEXT.md` glossary. Three prompts share "output rules" boilerplate via `prompts/pipeline.py` standards-injection.
- **Migration:** existing open issues unlabeled on this axis until manually triaged; iteration fail-fast surfaces them.
- **No worktree/orchestration changes** — slice mode is per-issue.
