# Global user-scope config and env layered with project-local

1. **Global = user-scope** via `platformdirs` (`~/.config/pycastle/` on Linux/Mac, `%APPDATA%\pycastle\` on Windows). Overridable via `PYCASTLE_HOME`. No machine-scope.
2. **`config.py` and `.env` are globalizable.** The fixed local `pycastle/Dockerfile` override path, fixed local `pycastle/prompts/` override path, and pycastle-managed `.gitignore` remain project-shaped; prompt overrides stay local and are not scaffolded.
3. **Layered merge.** `defaults → global → local`, field-by-field for `config.py`, per-key for `.env`. Process env tops `.env`.
4. **`pycastle init`** asks once ("global or local?"); accepts `--global` / `--local`. Never clobbers existing global files.
5. **Path fields forbidden globally.** `pycastle_dir`, `prompts_dir`, `logs_dir`, `worktrees_dir`, `env_file`, `dockerfile` raise `ConfigValidationError` if set globally.
6. **Single loader, all subcommands.** `run`, `labels`, `init` share one merged stack.
7. **Remote sourcing deferred.** Documented pattern: put `~/.config/pycastle/` under a personal git repo.
8. **`load_config(global_dir: Path | None = None)`.** Resolution: explicit arg → `PYCASTLE_HOME` → `platformdirs`.

## Reasons

- **Credentials are the high-value case** — reusing one OAuth token and `GH_TOKEN` across projects is the dominant pain point.
- **Layered merge composes** with existing field-by-field override; "global" is just a middle layer.
- **User-scope avoids shared-secret problems** that machine-scope creates on multi-user hosts.
- **Process-env-wins** matches conventional `.env` semantic; supports CI/CD secret stores.
- **Forbidding path fields globally** prevents silent project breakage from globalised `prompts_dir = Path("my-prompts")`.
- **Single loader** keeps subcommand behaviour predictable (`pycastle labels` is the second-largest credential consumer).
- **Deferring remote** keeps scope tight — sync needs concrete UX (auth, conflict resolution) before design.

## Consequences

- New term **`pycastle home`** and env var **`PYCASTLE_HOME`** enter the public surface.
- Missing local `config.py` / `.env` falls through to global, not straight to defaults.
- `load_config` no longer pure unless `global_dir` is passed; test fixtures must pass an explicit `global_dir`.
- Path field set globally → `ConfigValidationError` at load.
- Every CLI subcommand prints a one-line config-layer summary at startup.
- Multi-machine sync stays user-managed (dotfiles, `chezmoi`).
