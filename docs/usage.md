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

Interactive bootstrap for a consuming project. `pycastle init` syncs the pycastle-owned scaffold files that stay under package control, refreshes `config.py.example`, then runs the init wizard to choose service/scope, merge missing `.env` keys, and offer GitHub label setup.

Use `pycastle init --refresh` for the non-interactive scaffold sync path. It bypasses the wizard and refreshes the bundled scaffold files and `config.py.example` without changing `config.py`, `.env`, or any runtime session state. `cron.sh` invokes this on every tick so scaffold updates ship automatically when you upgrade pycastle.

Refresh behavior is asymmetric by design: `pycastle/config.py.example` is always refreshed, and `pycastle/setup/` is refreshed by both `pycastle init` and `pycastle init --refresh`. A global `config.py.example` is refreshed only if you already keep one in pycastle home.

```bash
pycastle init                # interactive bootstrap; asks where config.py / .env should live
pycastle init --local        # keep config.py and .env in ./pycastle/
pycastle init --global       # keep config.py and .env in pycastle home
pycastle init --refresh      # non-interactive scaffold sync; refreshes pycastle-owned files only
```

### `pycastle build`

Builds the universal agent image. If your project provides `pycastle/Dockerfile`, pycastle uses that local override; otherwise it builds from the bundled default Dockerfile. Pass `--no-cache` to force a clean build.

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
| `preflight_checks` | ruff, mypy, pytest | Commands run before planning |
| `implement_checks` | ruff fix, mypy, pytest | Commands the implementer must pass |
| `skip_preflight` | `False` | Set to `True` to bypass preflight entirely |
| `improve_mode` | `None` | `"until_sleep"` or `"endless"`; default for `pycastle run` when no `--improve` flag is passed |
| `improve_max` | `1` | Maximum improve-agent dispatches per run when improve mode is active |
| `plan_override` / `implement_override` / `review_override` / `merge_override` | — | Per-stage model and effort overrides |

Edit local `pycastle/config.py` and/or global `config.py` in pycastle home to tailor these to your project.

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

   This refreshes the bundled scaffold files that pycastle owns, including `setup/` and `config.py.example`, leaving your `config.py`, `.env`, and runtime session state untouched.

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
