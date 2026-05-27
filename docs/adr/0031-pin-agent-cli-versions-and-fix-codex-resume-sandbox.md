# Pin agent CLI versions and fix codex resume --sandbox rejection

The Codex CLI's `exec resume` subcommand does not accept `--sandbox` — sessions inherit sandbox config from the original run. `build_command` appended `--sandbox danger-full-access` unconditionally, so every codex resume attempt failed immediately. Fixed by emitting `--sandbox` only on `RunKind.FRESH`.

Separately, both agent CLIs (`@openai/codex` and Claude Code) were installed unpinned (`npm install -g @openai/codex`, `curl … | bash`), meaning any upstream release could silently break agent runs. Both default Dockerfiles now pin to tested versions: codex-cli 0.134.0, Claude Code 2.1.152.

## Considered Options

**Resume sandbox fix**: (a) pass via `-c sandbox=danger-full-access` on resume, (b) omit entirely. Chose (b) — the session already stores its sandbox config from the original fresh run; injecting it via `-c` is speculative and risks conflicting with stored session state.

**Version pinning location**: (a) hardcoded in Dockerfiles, (b) Python constants passed as Docker build args. Chose (a) — version pins are a build-time concern; consuming projects that need different versions override with their local Dockerfile copy. No templating overhead.
