# Prompt rendering lives in `AgentRunner`, not `ContainerRunner`

`ContainerRunner.work()` previously accepted nine parameters spanning two concerns: prompt rendering (`template`, `scope_args`, `renderer`, `send_role_prompt_on_resume`) and execution context (`role`, `run_kind`, `session_uuid`, `is_failsoft_recovery`). Inside `work()`, a three-way branch over `(run_kind, send_role_prompt_on_resume, is_failsoft_recovery)` decided which template to render — meaning the runner had to understand improve-mode session namespace semantics (ADR 0010) to decide what to write to `/tmp/.pycastle_prompt`. All prompt-rendering responsibility moves upstream into `AgentRunner.run()`: a private `_build_prompt()` helper resolves the prompt-shape contract into a single string per attempt, and `ContainerRunner.work()` now accepts `(prompt: str, *, role, run_kind, session_uuid)` — three keyword-only arguments, no renderer dependency.

## Considered Options

- **Status quo (nine parameters, rendering inside `ContainerRunner`).** Rejected: the runner cannot be tested without constructing a `PromptRenderer`, and prompt-shape policy lives behind a Docker substrate it does not need.
- **Two cohesive structs (`PromptSpec` + `WorkContext`) passed to a still-renders-internally `work()`.** Rejected: preserves the runner→ADR-0010 coupling and the three-way branch in the hardest place to test.
- **One composite struct absorbing all nine fields.** Rejected: groups two genuinely separate concerns and makes the rendering test surface no easier to isolate.
- **Extend `RunRequest` to absorb the grouping.** Rejected: `RunRequest` is the phase→`AgentRunner` contract, not the `AgentRunner`→`ContainerRunner` contract. Coupling the inner refactor to every phase caller conflicts with the out-of-scope rule.
- **Render inside `AgentRunner.run()` (chosen).** `AgentRunner.run()` already owns `run_kind` derivation and `is_failsoft_recovery` as locals. Rendering is a pure function of those locals plus `RunRequest` fields, so co-locating the render with the state that drives it removes the leakage.
- **Render in the phase (one level above `AgentRunner`).** Rejected: phases cannot know in advance whether fail-soft will trigger and would have to pre-render both prompt variants or expose a re-render callback.

## Consequences

- `ContainerRunner.work()` signature becomes `work(self, prompt: str, *, role: AgentRole, run_kind: RunKind = RunKind.FRESH, session_uuid: str | None = None) -> AgentOutput`. The `template`, `scope_args`, `renderer`, `send_role_prompt_on_resume`, and `is_failsoft_recovery` parameters are removed.
- `AgentRunner` gains `_build_prompt(template, scope_args, container_exec, *, run_kind, send_role_prompt_on_resume, is_failsoft_recovery) -> str` holding the three-way branch. Called once per attempt inside the retry loop, because both `run_kind` and `is_failsoft_recovery` can flip between attempts.
- `is_failsoft_recovery` no longer crosses the `AgentRunner`→`ContainerRunner` seam. It remains a local in `AgentRunner.run()` and an input to `_build_prompt`.
- `RunRequest` shape is unchanged for phase callers.
- `ContainerRunner` no longer holds a `PromptRenderer` reference. Tests for `ContainerRunner` no longer require a renderer fake. Tests for prompt-shape behaviour move to the `AgentRunner.run()` level.
- ADR 0009's `PromptRenderer` is unaffected in shape; its sole caller moves from `ContainerRunner.work()` to `AgentRunner._build_prompt()`. ADR 0010's session-namespace semantics no longer need to be understood by `ContainerRunner`.
