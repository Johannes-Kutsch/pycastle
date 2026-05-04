# ADR 0003: Global user-scope config and env layered with project-local

**Status:** Accepted
**Date:** 2026-05-04

## Context

Until now every consuming project carried its own `pycastle/config.py` and `pycastle/.env`. Operators running pycastle across several repos — and across machines (e.g. a remote Pi) — had to re-enter `GH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, and any cross-project preferences (`auto_push`, `STAGE_OVERRIDES`) in every project. Issue #470 asked for a single global location for config and env values, optionally remote, so changes propagate without per-project edits.

Several axes were genuine trade-offs:

- **Scope** — user-level (`~/.config/pycastle/`) vs. machine-level (`/etc/pycastle/`).
- **Files included** — `.env` only, `config.py` only, or both.
- **Precedence model** — full-replace (local file silences global) vs. layered merge (defaults → global → local, field-by-field).
- **Discovery** — hardcoded `platformdirs` path vs. env-var override vs. CLI flag.
- **Path-typed config fields** — allow them globally, forbid them, or warn.
- **Remote sourcing** — built-in sync command vs. defer to user-managed git.
- **Subcommand behaviour** — global applies everywhere or only to `pycastle run`.

## Decision

1. **Global = user-scope.** Resolved via `platformdirs` (`~/.config/pycastle/` on Linux/Mac, `%APPDATA%\pycastle\` on Windows). Overridable via the `PYCASTLE_HOME` env var. No machine-scope.
2. **`config.py` and `.env` are globalizable.** `Dockerfile`, `prompts/`, and `.gitignore` always scaffold locally because they are project-shaped.
3. **Layered merge.** `defaults → global → local`, field-by-field for `config.py`, per-key for `.env`. The process environment sits on top of the `.env` stack: `defaults → global .env → local .env → process env`.
4. **`pycastle init` asks once** ("global or local?") and accepts `--global` / `--local` flags to skip the prompt. Existing global files are never clobbered — if the global file already exists, init skips it with a message. Credential prompts are skipped only when the user picked `--global` *and* the credential already exists in global `.env`.
5. **Path-typed fields are forbidden in global `config.py`.** `pycastle_dir`, `prompts_dir`, `logs_dir`, `worktrees_dir`, `env_file`, `dockerfile` raise `ConfigValidationError` when set globally. Globalizing them is almost certainly a mistake.
6. **Single loader, all subcommands.** `run`, `labels`, `init` (post-prompt) and any future subcommand share one loader and one merged stack. A one-line layer summary (`Config: defaults + ~/.config/pycastle/config.py + pycastle/config.py`) prints at startup via `StatusDisplay.print("", ...)`.
7. **Remote sourcing deferred.** Documented pattern is "put `~/.config/pycastle/` under your own git repo". A built-in sync command is a separate issue if real demand emerges.
8. **`load_config` gains `global_dir: Path | None = None`.** Resolution: explicit arg → `PYCASTLE_HOME` → `platformdirs`. Tests pass a temp dir to fully sandbox; mirrors the existing `repo_root` parameter.

## Reasons

- **Credentials are the high-value case.** Reusing one OAuth token and one `GH_TOKEN` across every project on a machine (or every machine an operator uses) is the dominant pain point the issue calls out.
- **Layered merge composes with the existing model.** The loader already does field-by-field override of the defaults module by the local `config.py`. Inserting "global" as a middle layer requires no new mental model — full-replace would have forced operators to re-state every cross-project preference in every project that needs even one local tweak.
- **User-scope avoids shared-secret problems.** Pycastle is a single-developer tool driven by personal credentials. Machine-scope would force all users on a host to share `GH_TOKEN`/`CLAUDE_CODE_OAUTH_TOKEN`, which is the opposite of the desired property.
- **Process-env-wins is the conventional `.env` semantic.** It supports CI/CD secret stores and one-off ad-hoc overrides without file edits. Pycastle's variables are namespaced enough that accidental shadowing isn't a realistic risk.
- **Forbidding path fields globally prevents a foot-gun.** `prompts_dir = Path("my-prompts")` set globally would either be nonsensical or silently break projects that don't have that directory; raising at load time costs ~5 lines and is far clearer than a downstream "file not found".
- **Single loader keeps subcommand behaviour predictable.** `pycastle labels` is the second-largest credential consumer; splitting subcommand behaviour would re-introduce the original problem for it.
- **Deferring remote keeps scope tight.** Solving the global case alone covers ~80% of the pain. A built-in sync command needs concrete UX requirements (auth, secret handling, conflict resolution) to design against; speculative design risks the wrong abstraction.

## Consequences

- A new term **`pycastle home`** (the resolved global directory) and a new env var **`PYCASTLE_HOME`** enter the public surface.
- The `auto-discovery` definition broadens: project marker is unchanged (a `pycastle/` directory in CWD must exist), but missing local `config.py` / `.env` falls through to the global layer instead of straight to defaults.
- `load_config` is no longer pure with respect to env vars and `platformdirs` unless `global_dir` is passed explicitly. Test fixtures must pass an explicit `global_dir` (typically a temp dir or `Path("/nonexistent")`) to remain hermetic.
- Operators who previously stored credentials per-project keep working unchanged; layered merge is additive.
- An operator who sets a path field globally now sees a `ConfigValidationError` at load — surfacing the mistake at the boundary rather than as a downstream "file not found".
- Every CLI subcommand prints a one-line config-layer summary at startup, slightly increasing baseline output.
- Multi-machine config sharing remains a user-managed concern (e.g. dotfiles repo, `chezmoi`); pycastle does not own it.
