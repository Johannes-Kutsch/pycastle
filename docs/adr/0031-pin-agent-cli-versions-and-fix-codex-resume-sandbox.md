# Pin agent CLI versions and fix codex resume --sandbox rejection

The Codex CLI's `exec resume` subcommand does not accept `--sandbox` — sessions inherit sandbox config from the original run. `build_command` appended `--sandbox danger-full-access` unconditionally, so every codex resume attempt failed immediately. Fixed by emitting `--sandbox` only on `RunKind.FRESH`.

Separately, the supported agent CLIs (`@openai/codex`, Claude Code, and OpenCode) were installed unpinned (`npm install -g @openai/codex`, `curl … | bash`, or latest-equivalent), meaning any upstream release could silently break agent runs. The default Dockerfiles now pin tested versions: codex-cli 0.134.0, Claude Code 2.1.152, OpenCode CLI 1.15.12.

The same pinning policy applies to planned OpenCode support. `OpenCodeService` depends on the OpenCode CLI's JSON event shape, session-id resume contract, model selection flags, and usage-limit messages. Pycastle therefore pins the OpenCode CLI version in the agent image/runtime setup rather than installing whatever version is latest at build time.

## Considered Options

**Resume sandbox fix**: (a) pass via `-c sandbox=danger-full-access` on resume, (b) omit entirely. Chose (b) — the session already stores its sandbox config from the original fresh run; injecting it via `-c` is speculative and risks conflicting with stored session state.

**Version pinning location**: (a) hardcoded in Dockerfiles, (b) Python constants passed as Docker build args. Chose (a) — version pins are a build-time concern; consuming projects that need different versions override with their local Dockerfile copy. No templating overhead.

**OpenCode version policy**: (a) pin the OpenCode CLI like Claude and Codex, (b) install latest. Chose (a) — structured output and resume semantics are part of pycastle's runtime contract, so reproducibility matters more than automatically receiving upstream CLI changes.
