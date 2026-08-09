# Global user-scope config and env layered with project-local

> **Amended (#2081).** Item 5 originally forbade *path* fields globally. That clause no longer describes the code: ADR 0029 fixed the project-local layout, so `pycastle_dir`, `prompts_dir`, `worktrees_dir`, `env_file`, and `dockerfile` are now silently ignored in *both* layers rather than rejected in one, and `logs_dir` is explicitly globalizable (a global value is the parent directory for per-project logs). Item 5 is restated below as the rule that actually survives — global-forbidden means *project-derived identity*, not *path*. `dev_branch` and `working_branch` were members under ADR 0057 and are no longer; see that ADR.

1. **Global = user-scope** via `platformdirs` (`~/.config/pycastle/` on Linux/Mac, `%APPDATA%\pycastle\` on Windows). Overridable via `PYCASTLE_HOME`. No machine-scope.
2. **`config.py` and `.env` are globalizable.** The fixed local `pycastle/Dockerfile` override path, fixed local `pycastle/prompts/` override path, and pycastle-managed `.gitignore` remain project-shaped; prompt overrides stay local and are not scaffolded.
3. **Layered merge.** `defaults → global → local`, field-by-field for `config.py`, per-key for `.env`. Process env tops `.env`.
4. **`pycastle init`** asks once ("global or local?"); accepts `--global` / `--local`. Never clobbers existing global files.
5. **Project-derived identity fields forbidden globally.** A field raises `ConfigValidationError` when set in global `config.py` only if its default is derived from project identity, so a single global value would collide across every project. `docker_image_name` — derived from the sanitised repo directory name — is the only such field. Every other field is globalizable, with the project `config.py` overriding the global value.
6. **Single loader, all subcommands.** `run`, `labels`, `init` share one merged stack.
7. **Remote sourcing deferred.** Documented pattern: put `~/.config/pycastle/` under a personal git repo.
8. **`load_config(global_dir: Path | None = None)`.** Resolution: explicit arg → `PYCASTLE_HOME` → `platformdirs`.

## Reasons

- **Credentials are the high-value case** — reusing one OAuth token and `GH_TOKEN` across projects is the dominant pain point.
- **Layered merge composes** with existing field-by-field override; "global" is just a middle layer.
- **User-scope avoids shared-secret problems** that machine-scope creates on multi-user hosts.
- **Process-env-wins** matches conventional `.env` semantic; supports CI/CD secret stores.
- **Forbidding project-derived identity fields globally** prevents one global value silently colliding across projects — a globalised `docker_image_name` would have every repo build into the same image tag. A field whose default is a plain constant carries no such collision and stays globalizable.
- **Single loader** keeps subcommand behaviour predictable (`pycastle labels` is the second-largest credential consumer).
- **Deferring remote** keeps scope tight — sync needs concrete UX (auth, conflict resolution) before design.

## Consequences

- New term **`pycastle home`** and env var **`PYCASTLE_HOME`** enter the public surface.
- Missing local `config.py` / `.env` falls through to global, not straight to defaults.
- `load_config` no longer pure unless `global_dir` is passed; test fixtures must pass an explicit `global_dir`.
- Project-derived identity field set globally → `ConfigValidationError` at load.
- Every CLI subcommand prints a one-line config-layer summary at startup.
- Multi-machine sync stays user-managed (dotfiles, `chezmoi`).
