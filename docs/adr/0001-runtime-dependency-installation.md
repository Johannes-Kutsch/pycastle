# Runtime dependency installation over image bake-in

Dependency installation runs in the Setup phase (`pip install -e '.[dev]' || pip install -r requirements.txt`), not at image build time.

## Reasons

- **Always fresh.** `pyproject.toml` changes frequently; baked-in tools silently go stale unless the developer rebuilds.
- **No version skew.** Project-declared version wins at runtime anyway, making the image layer misleading.
- **General-purpose image.** Baking specific tool versions over-constrains consumers or forces frequent rebuilds.

## Consequences

- Any tool in `PREFLIGHT_CHECKS` must be declared in the consuming project's dependency file. Missing declaration surfaces as `command not found` at preflight.
- Slight startup cost: `pip install` runs on every container start.
