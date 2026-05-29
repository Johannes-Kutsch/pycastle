# Usage

Everything an adopter needs once they have decided to try pycastle: prerequisites, installation, CLI commands, configuration reference, pycastle home, and the upgrading walkthrough.

## Prerequisites

- Python 3.11.3 or later
- Docker (daemon must be running)
- A valid `CLAUDE_CODE_OAUTH_TOKEN` environment variable (or a `.env` file in your project root). Run `claude setup-token` to generate one.
- A GitHub repository with a `GH_TOKEN` environment variable that has issue read/write access. `GH_TOKEN` is the sole GitHub credential pycastle uses — there is no `gh` CLI dependency.

  If your repository belongs to an SSO-protected organisation, the PAT must be authorised for that org via the GitHub web UI (PAT settings → "Configure SSO" → Authorize) before pycastle can use it.

## Installation

```bash
pip install pycastle
```

## CLI Commands

### `pycastle init`

Interactive bootstrap for a consuming project. `pycastle init` syncs the pycastle-managed setup scaffold that stays under package control, refreshes `config.py.example`, then runs the init wizard to choose service/scope, merge missing `.env` keys, and offer GitHub label setup.

Use `pycastle init --refresh` for the non-interactive scaffold sync path. It bypasses the wizard and refreshes the pycastle-managed setup scaffold and `config.py.example` without changing `config.py`, `.env`, prompt overrides, Dockerfile overrides, or any runtime session state. `cron.sh` invokes this on every tick so scaffold updates ship automatically when you upgrade pycastle.

Refresh behavior is asymmetric by design: `pycastle/config.py.example` is always refreshed, and `pycastle/setup/` is refreshed by both `pycastle init` and `pycastle init --refresh`. Those two paths are pycastle-owned scaffold. Bundled prompts and the bundled universal Dockerfile remain the defaults unless you create local overrides. Local prompt files under `pycastle/prompts/` and `pycastle/Dockerfile` are user-owned overrides, so init never creates, overwrites, or deletes them. Your `config.py`, `.env`, prompt overrides, Dockerfile override, and `.pycastle-session/` runtime state stay yours across refreshes. A global `config.py.example` is refreshed only if you already keep one in pycastle home.

```bash
pycastle init                # interactive bootstrap; asks where config.py / .env should live
pycastle init --local        # keep config.py and .env in ./pycastle/
pycastle init --global       # keep config.py and .env in pycastle home
pycastle init --refresh      # non-interactive scaffold sync; refreshes pycastle-owned files only
```

### `pycastle build`

Builds the universal agent image. `pycastle build` uses `pycastle/Dockerfile` only if you created that file; otherwise it builds from the bundled universal Dockerfile. `pycastle init` and `pycastle init --refresh` do not create a local Dockerfile copy. Build selection does not scan for service-specific Dockerfiles under `pycastle/` or elsewhere in the repo: files such as `pycastle/Dockerfile.codex` or `pycastle/reviewer.Dockerfile` are ignored. The only local override path is `pycastle/Dockerfile`. Pass `--no-cache` to force a clean build.

```bash
pycastle build [--no-cache]
```

### `pycastle labels`

Creates the standard label set on your GitHub repository. These labels drive the triage workflow that feeds issues into the agent pipeline.

```bash
pycastle labels
```

### `pycastle run`

Runs the full agent pipeline. The pipeline iterates up to `max_iterations` times, each time picking up whatever `ready-for-agent` issues remain open. Progress is streamed to your terminal in real time.

```bash
pycastle run
pycastle run --improve              # dispatch the improve agent when no issues are ready (defaults to 'until_sleep')
pycastle run --improve endless      # keep generating improvements until Ctrl-C
```

Set `improve_mode = "until_sleep"` (or `"endless"`) in `pycastle/config.py` to make this the default for a repo without passing the flag every time — useful for the cron wrapper. The CLI flag overrides the config value.

## Pycastle Home

Pycastle home is the directory where global configuration (`config.py`, `.env`) is stored when you use `pycastle init --global`.

- **Linux/macOS:** `~/.config/pycastle/`
- **Windows:** `%APPDATA%\pycastle\`

Override the location with the `PYCASTLE_HOME` environment variable.

For multi-machine sync, put your pycastle home under your own dotfiles repository (e.g. via `chezmoi` or a plain git checkout). Pycastle does not own remote sourcing.

If `config.py.example` exists in pycastle home already, both `pycastle init` and `pycastle init --refresh` refresh it there as well. Otherwise the example file is only guaranteed locally at `pycastle/config.py.example`.

## Runtime Session State

`.pycastle-session/` is runtime-only state rooted at the mounted worktree, not inside `pycastle/`, and it is not created by `pycastle init`. Pycastle uses it for per-role resume state and provider-specific session data while runs are in progress.

Codex authentication is seeded at runtime only: when a fresh Codex role state dir is missing `auth.json`, pycastle copies the host's `~/.codex/auth.json` into that role's `.pycastle-session/.../codex/` directory before launch.

## Configuration

Runtime configuration lives in `config.py`, loaded from pycastle home first and then from local `pycastle/config.py` when present. Key settings:

| Setting | Default | Description |
|---|---|---|
| `max_iterations` | `10` | How many plan→implement→merge loops to run |
| `max_parallel` | `1` | Maximum concurrent implementer agents |
| `issue_label` | `ready-for-agent` | Label the planner filters on |
| `hitl_label` | `ready-for-human` | Label that triggers a human-intervention exit |
| `logs_dir` | `pycastle/logs` | Log directory. In global config this is a parent directory and the effective project log directory is `<logs_dir>/<sanitised project name>/`; in local config the configured path is used directly as the effective log directory. Agent logs, `errors.log`, and cron wrapper output all go to that effective project log directory |
| `preflight_checks` | ruff, mypy, pytest | Commands run before planning |
| `implement_checks` | ruff fix, mypy, pytest | Commands the implementer must pass |
| `skip_preflight` | `False` | Set to `True` to bypass preflight entirely |
| `improve_mode` | `None` | `"until_sleep"` or `"endless"`; default for `pycastle run` when no `--improve` flag is passed |
| `improve_max` | `1` | Maximum improve-agent dispatches per run when improve mode is active |
| `plan_override` / `implement_override` / `review_override` / `merge_override` | — | Per-stage model and effort overrides |

Edit local `pycastle/config.py` and/or global `config.py` in pycastle home to tailor these to your project. Project-local layout paths are fixed: local config lives at `pycastle/config.py`, local `.env` at `pycastle/.env`, prompt overrides at `pycastle/prompts/`, worktrees under `pycastle/.worktrees/`, setup scaffold under `pycastle/setup/`, and the optional Dockerfile override at `pycastle/Dockerfile`. Ownership is split on purpose: pycastle manages `setup/` and `config.py.example`, while `config.py`, `.env`, prompt overrides, the optional Dockerfile override, and `.pycastle-session/` runtime state are user-owned.

The ownership rule is defaults-first: if you do not create a local override, pycastle keeps using the bundled default. That applies to prompts and to the universal Dockerfile. `pycastle/prompts/` is therefore an override layer containing only the prompt files you chose to fork, not a mirror of the bundled prompt tree.

### Prompt overrides

Bundled prompts live in pycastle's installed defaults and are used automatically. If `pycastle/prompts/` does not exist, that is the normal defaults-first state and pycastle reads every prompt from the bundled set. To customize one prompt or shared prompt fragment, create a matching file under `pycastle/prompts/` using the same relative path as the bundled default. Missing local files fall back to bundled defaults per file, so `pycastle/prompts/` is a per-file override layer rather than a separate prompt tree you have to copy wholesale.

`pycastle init` and `pycastle init --refresh` do not create `pycastle/prompts/` or copy prompt files. Existing local prompt files are user-owned overrides and shadow bundled prompt improvements until you update or remove them. The practical workflow is: inspect the bundled prompt first, then create one local override only for the file you actually want to fork.

To inspect the current bundled prompt before creating an override, first open the installed package's `pycastle/defaults/prompts/` file or the source tree copy under `src/pycastle/defaults/prompts/`. Then copy only that single file into `pycastle/prompts/` at the same relative path and edit the local copy. Do not start by copying the whole prompts tree unless you intend to own every file in it.

### Logs

`logs_dir` has different semantics depending on where you set it. In local `pycastle/config.py`, `logs_dir` is the exact log directory for that consuming project. In global `config.py` under pycastle home, `logs_dir` is a parent directory; pycastle then creates and uses `<logs_dir>/<sanitised project name>/` as that consuming project's effective log directory.

Everything that writes project logs uses that effective project log directory, including agent logs, `errors.log`, and cron wrapper output. If you switch from local to global config, expect the log path to change from "direct path" semantics to "parent directory per project" semantics.

### Minimal local `config.py`

To enable `improve` mode for the cron tick without touching anything else, create (or edit) `pycastle/config.py` in the project root.

Create from scratch:

```bash
cat > pycastle/config.py <<'EOF'
improve_mode = "until_sleep"
improve_max = 1
EOF
```

Or edit an existing one and add the same two lines:

```bash
nano pycastle/config.py
```

Only the keys you set override defaults — everything else stays on the bundled values. Drop `improve_max` if you want unlimited improve dispatches until the next sleep.

## Upgrading pycastle on a deployed host

Run these steps inside each consuming project on the host (e.g. over SSH on the pi):

1. **Enter the project and activate its venv**

   ```bash
   cd <project-dir>
   source .venv/bin/activate
   ```

2. **Update pycastle**

   ```bash
   pip install --upgrade pycastle
   # or pin: pip install --upgrade 'pycastle==<version>'
   ```

3. **Refresh the bundled defaults**

   ```bash
   pycastle init --refresh
   ```

   This refreshes the pycastle-managed scaffold files, including `setup/` and `config.py.example`, leaving your `config.py`, `.env`, prompt overrides, Dockerfile override, and runtime session state untouched.

4. **Remove the existing cronjob**

   The uninstall script keys off a marker derived from the project's absolute path, so it only touches that project's line:

   ```bash
   bash pycastle/setup/cron-uninstall.sh
   ```

5. **Install the new cronjob**

   ```bash
   bash pycastle/setup/cron-install.sh
   ```

6. **Verify**

   ```bash
   crontab -l | grep pycastle
   ```

   You should see one line per project, each ending with `# pycastle:<absolute-project-path>`.
