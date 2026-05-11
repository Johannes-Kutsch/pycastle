# Global user-scope config and env layered with project-local

1. **Global = user-scope.** Resolved via `platformdirs` (`~/.config/pycastle/` on Linux/Mac, `%APPDATA%\pycastle\` on Windows). Overridable via `PYCASTLE_HOME`. No machine-scope.
2. **`config.py` and `.env` are globalizable.** `Dockerfile`, `prompts/`, and `.gitignore` always scaffold locally because they are project-shaped.
3. **Layered merge.** `defaults → global → local`, field-by-field for `config.py`, per-key for `.env`. The process environment sits on top: `defaults → global .env → local .env → process env`.
4. **`pycastle init` asks once** ("global or local?") and accepts `--global` / `--local` flags. Existing global files are never clobbered. Credential prompts skipped only when `--global` and the credential already exists in global `.env`.
5. **Path-typed fields are forbidden in global `config.py`.** `pycastle_dir`, `prompts_dir`, `logs_dir`, `worktrees_dir`, `env_file`, `dockerfile` raise `ConfigValidationError` when set globally.
6. **Single loader, all subcommands.** `run`, `labels`, `init` and future subcommands share one loader and one merged stack.
7. **Remote sourcing deferred.** Documented pattern: put `~/.config/pycastle/` under a personal git repo. Built-in sync is a separate issue.
8. **`load_config` gains `global_dir: Path | None = None`.** Resolution: explicit arg → `PYCASTLE_HOME` → `platformdirs`. Tests pass a temp dir to sandbox.

## Reasons

- **Credentials are the high-value case.** Reusing one OAuth token and `GH_TOKEN` across every project on a machine is the dominant pain point.
- **Layered merge composes with the existing model.** The loader already does field-by-field override of defaults by local `config.py`. Inserting "global" as a middle layer requires no new mental model.
- **User-scope avoids shared-secret problems.** Machine-scope would force all users on a host to share `GH_TOKEN`/`CLAUDE_CODE_OAUTH_TOKEN`.
- **Process-env-wins is the conventional `.env` semantic.** Supports CI/CD secret stores and one-off overrides without file edits.
- **Forbidding path fields globally prevents a foot-gun.** `prompts_dir = Path("my-prompts")` set globally would silently break projects that don't have that directory; raising at load time is far clearer than a downstream "file not found".
- **Single loader keeps subcommand behaviour predictable.** `pycastle labels` is the second-largest credential consumer; splitting subcommand behaviour would re-introduce the original problem for it.
- **Deferring remote keeps scope tight.** Solving the global case covers ~80% of the pain. A built-in sync command needs concrete UX requirements (auth, conflict resolution) to design against.

## Consequences

- A new term **`pycastle home`** (the resolved global directory) and env var **`PYCASTLE_HOME`** enter the public surface.
- Missing local `config.py` / `.env` falls through to the global layer instead of straight to defaults.
- `load_config` is no longer pure unless `global_dir` is passed explicitly. Test fixtures must pass an explicit `global_dir` to remain hermetic.
- Operators storing credentials per-project keep working unchanged; layered merge is additive.
- An operator who sets a path field globally sees `ConfigValidationError` at load.
- Every CLI subcommand prints a one-line config-layer summary at startup.
- Multi-machine config sharing remains user-managed (dotfiles, `chezmoi`); pycastle does not own it.
