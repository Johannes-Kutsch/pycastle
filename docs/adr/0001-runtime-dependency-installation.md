# ADR 0001: Runtime dependency installation over image bake-in

**Status:** Accepted  
**Date:** 2026-05-01

## Context

The default Dockerfile ships with the Python runtime, system utilities (git, gh), and the Claude Code CLI. It does not include any consuming project dev tools (ruff, mypy, pytest, etc.).

The default `PREFLIGHT_CHECKS` run `ruff check .`, `mypy .`, and `pytest` inside the agent container. These tools must be available before the Pre-flight phase runs.

Two approaches were considered:

**Option A — Bake into the image at build time:**  
`pycastle build` uses the consuming project's root as the Docker build context, so `pyproject.toml` is accessible. The Dockerfile could `COPY pyproject.toml` and run `pip install ".[dev]"` during the image build.

**Option B — Install at runtime during the Setup phase:**  
`_setup` runs `pip install -e '.[dev]' || pip install -r requirements.txt` after the container starts and the worktree is mounted. Tools and project dependencies are always installed fresh from the current `pyproject.toml`.

## Decision

**Option B.** Dependency installation happens at runtime in the Setup phase, not at image build time.

## Reasons

- **Always fresh.** `pyproject.toml` changes frequently during active development. Option A would silently run agents with stale tools unless the developer remembers to run `pycastle build` after every dependency change. Option B is always consistent with the current `pyproject.toml`.
- **No version skew.** Baking tools into the image creates two sources of truth: the image-installed version and the project-declared version. The project-declared version wins at runtime anyway if both are present, making the image layer misleading.
- **General-purpose image.** The default image must work for any consuming project. Baking in a specific version of ruff or pytest would either over-constrain consuming projects or require frequent image rebuilds as tools evolve.

## Consequences

- Any tool referenced in `PREFLIGHT_CHECKS` must be declared in the consuming project's dependency file (`[dev]` extras in `pyproject.toml`, or `requirements.txt`). The image does not provide dev tools as a fallback. A missing declaration surfaces as `command not found` at preflight time.
- Agent startup is slightly slower because `pip install` runs on every container start.
- The Setup phase owns dependency installation. The Prepare phase handles only prompt rendering and injection.
