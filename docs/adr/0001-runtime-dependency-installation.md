# Runtime dependency installation over image bake-in

**Option B.** Dependency installation happens at runtime in the Setup phase (`pip install -e '.[dev]' || pip install -r requirements.txt`), not at image build time.

## Reasons

- **Always fresh.** `pyproject.toml` changes frequently during active development. Option A would silently run agents with stale tools unless the developer remembers to run `pycastle build` after every dependency change. Option B is always consistent with the current `pyproject.toml`.
- **No version skew.** Baking tools into the image creates two sources of truth: the image-installed version and the project-declared version. The project-declared version wins at runtime anyway if both are present, making the image layer misleading.
- **General-purpose image.** The default image must work for any consuming project. Baking in a specific version of ruff or pytest would either over-constrain consuming projects or require frequent image rebuilds as tools evolve.

## Consequences

- Any tool referenced in `PREFLIGHT_CHECKS` must be declared in the consuming project's dependency file (`[dev]` extras in `pyproject.toml`, or `requirements.txt`). The image does not provide dev tools as a fallback. A missing declaration surfaces as `command not found` at preflight time.
- Agent startup is slightly slower because `pip install` runs on every container start.
- The Setup phase owns dependency installation. The Prepare phase handles only prompt rendering and injection.
