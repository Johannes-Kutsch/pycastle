# Prompt rendering lives in `AgentRunner`, not `ContainerRunner`

`ContainerRunner.work()` previously accepted nine parameters spanning two concerns: prompt rendering (`template`, `scope_args`, `renderer`, `send_role_prompt_on_resume`) and execution context (`role`, `run_kind`, `session_uuid`, `is_failsoft_recovery`). Inside `work()`, a three-way branch over `(run_kind, send_role_prompt_on_resume, is_failsoft_recovery)` decided which template to render — meaning the runner had to understand improve-mode session namespace semantics (ADR 0010) to decide what to write to `/tmp/.pycastle_prompt`.

All prompt rendering moves upstream into `AgentRunner.run()`: a private `_build_prompt()` resolves the prompt-shape contract into a single string per attempt. `ContainerRunner.work()` now takes `(prompt: str, *, role, run_kind, session_uuid, on_thread_id=None)`. The optional callback is a service side channel for Codex's server-generated thread ID; it does not reintroduce prompt rendering into `ContainerRunner`.

## Considered Options

- **Nine parameters, rendering inside `ContainerRunner` (status quo).** Rejected: runner cannot be tested without a `PromptRenderer`; prompt-shape policy lives behind a Docker substrate it doesn't need.
- **Two cohesive structs (`PromptSpec` + `WorkContext`).** Rejected: preserves runner→ADR-0010 coupling and the three-way branch in the hardest place to test.
- **One composite struct absorbing all nine fields.** Rejected: groups two separate concerns and doesn't isolate the rendering test surface.
- **Extend `RunRequest` to absorb the grouping.** Rejected: `RunRequest` is the phase→`AgentRunner` contract, not the `AgentRunner`→`ContainerRunner` contract.
- **Render inside `AgentRunner.run()` — chosen.** `AgentRunner.run()` already owns `run_kind` derivation and `is_failsoft_recovery` as locals; co-locating render with state that drives it removes the leakage.
- **Render in the phase (one level above).** Rejected: phases can't predict fail-soft and would pre-render both variants or expose a re-render callback.

## Consequences

- `ContainerRunner.work(self, prompt: str, *, role: AgentRole, run_kind: RunKind = RunKind.FRESH, session_uuid: str | None = None, on_thread_id: Callable[[str], None] | None = None) -> AgentOutput`. `template`, `scope_args`, `renderer`, `send_role_prompt_on_resume`, `is_failsoft_recovery` removed.
- `AgentRunner._build_prompt(template, scope_args, container_exec, *, run_kind, send_role_prompt_on_resume, is_failsoft_recovery) -> str` holds the three-way branch; called once per attempt inside the retry loop (both flags can flip between attempts).
- `is_failsoft_recovery` no longer crosses the `AgentRunner`→`ContainerRunner` seam.
- `RunRequest` shape unchanged for phase callers.
- `ContainerRunner` no longer holds a `PromptRenderer` reference; tests no longer need a renderer fake. Prompt-shape tests move to the `AgentRunner.run()` level.
- ADR 0009's `PromptRenderer` shape unaffected; sole caller moves to `AgentRunner._build_prompt()`. ADR 0010's session-namespace semantics no longer need to be understood by `ContainerRunner`.
