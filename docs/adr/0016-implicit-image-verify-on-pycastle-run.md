# Implicit Docker image verify on `pycastle run`

`pycastle run` previously assumed the image existed and was current; users had to remember `pycastle build` after editing `Dockerfile`. Failure mode is silent: stale image runs, agent behaves subtly wrong, wasted iteration. `run` now invokes the same `docker build` path as `pycastle build` first; Docker's layer cache makes the no-op case cheap (~1–3s). Explicit `pycastle build` stays — home for `--no-cache` and Dockerfile validation without a full run.

## Considered Options

- **User must remember (status quo).** Rejected: the foot-gun this removes; cost is one wasted run in tokens and wall time.
- **Existence check only.** Rejected: solves "missing on fresh clone" but not "exists but stale".
- **Hash build context ourselves.** Rejected: reimplements Docker's cache-invalidation; easy to omit a `COPY`'d file.
- **Always invoke `docker build`, rely on layer cache — chosen.** Daemon roundtrip is the only cost when nothing changed.
- **Verify inside `orchestrator.run` / `ContainerRunner.work`.** Rejected: pulls `DockerService` into orchestrator; tangles build output with live status; verify is a CLI-contract property.
- **Add `--no-build` opt-out / `--no-cache` on `run`.** Rejected: `--no-build` invites "always pass it" recreating stale-image problem; `--no-cache` belongs on `build`.

## Consequences

- `pycastle run` calls same build path as `pycastle build` first; failures abort `run` with build error, no stale-image fallback.
- Cache-hit path: build output suppressed; one-liner confirms verify ran. Cache-miss: build progress streams via status row (layer rebuilds can take minutes).
- `pycastle build` unchanged; owns `--no-cache` and standalone Dockerfile check.
- `build_command.main()` stops calling `sys.exit` on success so it can be invoked in-process from `run_cmd`. Exit semantics move to CLI entry points.
- Docker daemon availability becomes precondition of every `run` (already was in practice).
