# Runtime dependency installation over image bake-in

Dependency installation runs in the Setup phase (`pip install -e '.[dev]' || pip install -r requirements.txt`), not at image build time. Consuming-project development tools therefore remain Setup-installed runtime dependencies, not tools baked into the Docker image.

## Reasons

- **Always fresh.** `pyproject.toml` changes frequently; baked-in tools silently go stale unless the developer rebuilds.
- **No version skew.** Project-declared version wins at runtime anyway, making the image layer misleading.
- **General-purpose image.** Baking specific tool versions over-constrains consumers or forces frequent rebuilds.

## Consequences

- Any tool in `PREFLIGHT_CHECKS` that pycastle expects to come from Python packaging must be declared in the consuming project's dependency metadata (`pyproject.toml` or `requirements.txt`).
- If such a Python-declared tool is still unavailable when the check command runs, pycastle treats that as a broken Setup/toolchain contract and routes it as an upstream Setup failure rather than an ordinary preflight check failure.
- Ordinary preflight check failures remain the path for commands that start successfully and then return a non-zero result because the consuming project's source or environment failed the check itself.
- Non-Python package managers remain outside this classification until pycastle owns a Setup-phase contract for installing and verifying them.
- Slight startup cost: `pip install` runs on every container start.
