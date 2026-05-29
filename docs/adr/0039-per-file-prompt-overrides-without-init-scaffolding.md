# Per-file prompt overrides without init scaffolding

Bundled prompts are the base layer, and local prompt files are per-file overrides at the same relative path under `pycastle/prompts/`. The rule applies to role prompts, reference files, and shared prompt fragments such as `shared/_issue-tracker.md` and shared standards files. `pycastle init` and `pycastle init --refresh` do not create local prompt files or an empty prompts directory; absence of `pycastle/prompts/` means every prompt is rendered from bundled defaults.

The alternative was to keep copying bundled prompts into consuming projects, or to treat a local prompts directory as an all-or-nothing custom tree. Per-file override preserves the ability to customize one prompt while avoiding stale local copies that shadow fixed defaults after upgrades.
