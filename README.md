# pycastle

pycastle is a label-driven orchestrator for agentic coding work: label a GitHub issue `ready-for-agent`, and a pipeline picks it up, implements it, reviews it, and merges it.

## Supported agents

Currently ships with Claude Code support; Codex planned.

## Why pycastle

- **Unattended operation.** Once issues are triaged, pycastle runs the full pipeline without anyone at the keyboard — overnight, on a Raspberry Pi, or scheduled via cron.
- **Deliberate human-in-the-loop gate.** The `ready-for-agent` label is set by a person (or a triage agent). Nothing enters automation until a human — or a designated triage step — explicitly approves it.
- **Inspectable phase boundaries.** Each phase (preflight, plan, implement, review, merge) is discrete and logged, so you can see exactly where a run succeeded or stalled.
- **Parallel issues without conflict.** Multiple issues are implemented in isolated worktrees and merged in a single phase; when conflicts arise a merger agent resolves them rather than blocking the whole run.

## The pipeline

### Preflight
Before any agent work begins, pycastle runs the configured checks (linting, type-checking, tests) against the current codebase. Preflight exists because agents should start from a green baseline — handing an agent a broken repo compounds errors rather than fixing them. If a check fails, a preflight-issue agent diagnoses the failure and files a structured GitHub issue, routing it to either `ready-for-agent` or `ready-for-human` depending on whether automation can fix it.

### Plan
The planner agent reads all open `ready-for-agent` issues, evaluates declared dependencies, filters out anything still blocked, and produces an ordered list of issues to tackle this iteration. The plan phase exists to prevent agents from starting work that depends on unfinished prerequisites.

### Implement
An implementer agent is spawned for each planned issue in an isolated worktree. The agent reads the issue, writes the code, and runs the implement checks in a feedback loop until they pass. The implement phase exists because isolating each issue's changes prevents one in-progress fix from interfering with another.

### Review
Immediately after each implementer completes, a reviewer agent inspects the same branch — re-running checks, reading the diff, and pushing corrections directly onto the branch. The review phase exists because separating the implementer and reviewer reduces accept-your-own-work bias and catches mistakes that the implementer's own feedback loop missed.

### Merge 
Once all implementer/reviewer pairs have finished, pycastle merges each completed branch into the default branch and closes the corresponding GitHub issue. When branches conflict, a merger agent resolves them before committing. The merge phase exists as a dedicated integration step so that conflict resolution is handled consistently and issues are only closed after code is confirmed on the main branch.

## Labels

`ready-for-agent` is the entry point into automation. Labelling an issue `ready-for-agent` is a deliberate act: it means the issue is fully specified, has clear acceptance criteria, and needs no further clarification before an agent can work on it. The planner only considers issues carrying this label — everything else is invisible to the pipeline. If preflight checks fail in a way that requires human attention, the auto-filed issue is labelled `ready-for-human` instead, and the pipeline pauses until a person re-routes it.

## Getting started

- **Install, CLI, and configuration:** [`docs/usage.md`](docs/usage.md)
- **Unattended / cron operation:** [`docs/cron-setup.md`](docs/cron-setup.md)

## Acknowledgements

Initial inspiration: [sandcastle](https://github.com/mattpocock/sandcastle) by Matt Pocock.
