# pycastle

pycastle is a Python orchestrator for autonomous [Claude Code](https://claude.ai/code) agents running inside Docker containers. It is inspired by [sandcastle](https://github.com/mattpocock/sandcastle) — Matt Pocock's original project — and brings the same multi-agent, worktree-based workflow to a pip-installable Python package with configurable prompts, Dockerfile, and environment.

## Installation

```bash
pip install pycastle
```

## Prerequisites

- Python 3.11.3 or later
- Docker (daemon must be running)
- A valid `CLAUDE_CODE_OAUTH_TOKEN` environment variable (or a `.env` file in your project root). Run `claude setup-token` to generate one.
- A GitHub repository with a `GH_TOKEN` environment variable that has issue read/write access. `GH_TOKEN` is the sole GitHub credential pycastle uses — there is no `gh` CLI dependency.

  If your repository belongs to an SSO-protected organisation, the PAT must be authorised for that org via the GitHub web UI (PAT settings → "Configure SSO" → Authorize) before pycastle can use it.

## CLI Commands

### `pycastle init`

Copies the default `pycastle/` configuration directory into your project root. This directory contains the `Dockerfile`, `config.py`, prompt templates that drive the agents (`plan-prompt.md`, `implement/behavior.md`, `implement/refactor.md`, `implement/docs.md`, `review-prompt.md`, `merge-prompt.md`, plus coding standards under `prompts/coding-standards/`), and the cron wrappers (`setup/cron.sh`, `setup/cron-install.sh`, `setup/cron-uninstall.sh`). Run this once per repository, then customise the files to suit your project.

Pass `--refresh` to re-copy every bundled default over the existing files, leaving `config.py` and `.env` untouched. `cron.sh` invokes this on every tick so bug fixes ship automatically when you upgrade pycastle.

```bash
pycastle init                # asks once: scaffold config.py / .env globally or locally?
pycastle init --local        # write everything to ./pycastle/ (legacy behaviour)
pycastle init --global       # write config.py and .env to pycastle home; project-shaped files (Dockerfile, prompts/, .gitignore) still go local
```

Pycastle home defaults to `~/.config/pycastle/` on Linux/macOS and `%APPDATA%\pycastle\` on Windows. Override with the `PYCASTLE_HOME` environment variable.

For multi-machine sync, put your pycastle home under your own dotfiles repository (e.g. via `chezmoi` or a plain git checkout). Pycastle does not own remote sourcing.

### `pycastle build`

Builds the Docker image defined in `pycastle/Dockerfile`. Pass `--no-cache` to force a clean build. You must rebuild whenever you change the Dockerfile or install new dependencies.

```bash
pycastle build [--no-cache]
```

### `pycastle labels`

Creates the standard label set on your GitHub repository. These labels drive the triage workflow that feeds issues into the agent pipeline (see [The `ready-for-agent` label](#the-ready-for-agent-label) below).

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

## Unattended operation

Every `pycastle init` (and `pycastle init --refresh`) scaffolds three host-side cron helpers into your project's `pycastle/setup/` directory:

- `setup/cron.sh` — bootstrap-and-run wrapper. Acquires a global flock at `$PYCASTLE_HOME/.cron.lock` (6-hour timeout) so multiple repos on the same host serialize cleanly, asserts `.venv/` exists, upgrades pycastle, reinstalls the consuming project, runs `pycastle init --refresh`, rebuilds the Docker image, invokes `pycastle run`, then trims `cron.log` to the last 10000 lines so it cannot grow unbounded.
- `setup/cron-install.sh` — idempotently installs a daily entry (`0 1 * * *`) into your user crontab, tagged `# pycastle:<absolute-repo-path>` so multiple repos coexist. The crontab line redirects stdout+stderr into `<logs_dir>/cron.log` (resolved from the project's `pycastle/config.py` at install time), so cron output lands alongside the per-agent logs and is captured from second 0 — including bootstrap failures like a missing `.venv/` or pip errors.
- `setup/cron-uninstall.sh` — removes only the line bearing this repo's marker.

Point `logs_dir` somewhere outside the repo (e.g. a Syncthing-shared directory) in each project's `pycastle/config.py` to ship cron + agent logs off the host automatically:

```python
from pathlib import Path
logs_dir = Path.home() / "Syncthing" / "pycastle-cron-logs" / "<project-name>"
```

Re-run `bash pycastle/setup/cron-install.sh` after changing `logs_dir` so the new path is baked into the crontab line.

For a step-by-step cron setup, see [`docs/cron-setup.md`](docs/cron-setup.md).

### Upgrading pycastle on a deployed host

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

   This re-copies the bundled `setup/` scripts, prompts, and Dockerfile, leaving your `config.py` and `.env` untouched.

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

#### Minimal local `config.py`

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

## How the pipeline works

Each iteration of `pycastle run` moves through five phases in order.

### 1. Pre-flight

Before any agent work starts, pycastle runs the configured preflight checks inside Docker (default: `ruff check .`, `mypy .`, `pytest`). If all checks pass the pipeline continues normally.

If a check fails, a **preflight-issue agent** is spawned. It analyses the failure and creates a GitHub issue describing what needs to be fixed. The issue is labelled either `ready-for-agent` (the fix can be automated) or `ready-for-human` (a human must intervene). If the issue is `ready-for-human`, pycastle exits immediately so you can investigate. If it is `ready-for-agent`, the issue is queued as the sole work item for this iteration and the pipeline continues.

You can skip preflight entirely by setting `skip_preflight = True` in `pycastle/config.py` — useful while the codebase is still being bootstrapped.

### 2. Planner

The **planner agent** reads all open GitHub issues labelled `ready-for-agent`. It evaluates dependencies (issues can declare `blocked by #N` in their body), filters out issues that are still blocked, and emits a `<plan>` JSON block listing the unblocked issues to tackle this iteration. Only the issues selected by the planner proceed to the next phase.

### 3. Implementer(s)

For each planned issue, pycastle spawns an **implementer agent** in a dedicated git worktree on a branch named `pycastle/issue-<N>`. The agent reads the issue, writes the code, and continuously runs the implement checks (`ruff check --fix`, `ruff format --check`, `mypy .`, `pytest` by default) until they pass. When the agent is satisfied it emits `<promise>COMPLETE</promise>`; if it cannot complete the issue it exits without that tag and the issue is skipped this iteration.

Multiple implementer agents can run in parallel (controlled by `max_parallel` in `config.py`).

### 4. Reviewer

Immediately after each implementer signals completion, a **reviewer agent** inspects the same branch. The reviewer re-runs the checks, reads the diff, and pushes any corrections directly onto the branch before handing off to the merge phase.

### 5. Merging

Once all implementer/reviewer pairs have finished, pycastle attempts to fast-forward merge each completed branch into the default branch:

- **No conflict** — the branch is merged automatically and the worktree is cleaned up.
- **Conflict** — the conflicting branches are handed to a **merger agent**, which resolves the conflicts manually, re-runs the preflight checks, and commits the result.

After merging, the corresponding GitHub issue is closed. If an issue is a child of a parent/epic issue and all sibling issues are now closed, the parent issue is closed too. Merged branches are deleted.

If the working tree has uncommitted changes when the merge phase begins, pycastle waits (polling every 10 seconds) until the tree is clean before proceeding.

## The `ready-for-agent` label

`ready-for-agent` is the entry point into the automated pipeline. The planner only considers issues that carry this label. The intended workflow is:

1. A new issue arrives labelled `needs-triage`.
2. A maintainer (or a separate triage agent) evaluates it and either closes it, marks it `need-info`, `wontfix`, or — once it is fully specified with a clear acceptance criterion — relabels it `ready-for-agent`.
3. On the next `pycastle run`, the planner picks it up and the automated pipeline takes over.

Marking an issue `ready-for-agent` is a deliberate gate: it signals that the issue has enough detail for an agent to implement it without further clarification.

The companion label `ready-for-human` is used for issues that are too ambiguous, require access to external systems, or have failed preflight in a way that needs manual diagnosis. These issues are never picked up by the planner.

## Configuration

All runtime configuration lives in `pycastle/config.py`. Key settings:

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

Edit `pycastle/config.py` (created by `pycastle init`) to tailor these to your project.
