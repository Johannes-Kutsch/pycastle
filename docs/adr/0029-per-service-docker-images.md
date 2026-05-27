# Per-service Docker images

`pycastle build` was service-blind — one image from `cfg.dockerfile` regardless of `referenced_services(cfg)`. Adding Codex to config after init left the container without Node.js/Codex CLI, causing silent runtime failures (#925, #931). We split into two separate images: `Dockerfile.claude` (Claude CLI only) and `Dockerfile.codex` (Node.js + Codex CLI only, no Claude CLI). `docker_image_name` becomes a base prefix; build tags `<base>-claude` and `<base>-codex` per referenced service. Runtime selects image per agent role from `StageOverride.service`. The `dockerfile` config field is dropped (silently ignored if still set).

## Considered Options

- **Single dynamic Dockerfile with ARG/multi-stage.** Rejected: one complex Dockerfile harder to reason about than two simple ones; conditional `RUN` layers interact poorly with Docker cache.
- **Combined image with both toolchains baked in.** Rejected: this is what `Dockerfile.claude-codex` was — larger image, installs tools the agent never uses, source of the original bug.
- **Two separate images, one per service — chosen.** Each image contains exactly what its service needs. Build resolves Dockerfiles per service; init seeds only referenced services; runtime picks image from stage override.

## Consequences

- `Dockerfile.claude-codex` deleted. New `Dockerfile.codex` is codex-only.
- `pycastle init` seeds `Dockerfile.claude` and/or `Dockerfile.codex` to `pycastle/` based on service selection. `--refresh` adds newly-referenced Dockerfiles.
- Dockerfile resolution: local `pycastle/Dockerfile.<service>` → bundled default. Exposed as `resolve_dockerfile(service, pycastle_dir)`.
- `pycastle build` loops `referenced_services(cfg)`, resolves each Dockerfile, builds `<docker_image_name>-<service>`. Per-image progress output.
- `RunRequest` gains `service` field. Each call site threads `StageOverride.service`. `AgentRunner` accepts a service registry, selects image and `AgentService` per request.
- `cfg.dockerfile` field dropped from `Config`. Silently ignored if set in user config (no error).
- `_GLOBAL_FORBIDDEN_FIELDS` loses `dockerfile`.
- Cross-service fallback: both images built when both services appear anywhere in config (including fallback chains).
