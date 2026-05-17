# Implicit Docker image verify on `pycastle run`

`pycastle run` previously assumed the image named by `docker_image_name` already exists and is current; users were expected to remember to invoke `pycastle build` after editing the `Dockerfile`, bumping `.python-version`, or changing anything else the image bakes in. The failure mode is silent: a stale image runs, the agent behaves subtly wrong, and the divergence is only noticed after a wasted iteration. `run` now invokes the same `docker build` path as `pycastle build` before doing anything else; Docker's layer cache makes the no-op case cheap (~1–3s) and the always-correct case automatic. The explicit `pycastle build` command stays — it remains the home for `--no-cache` and for validating the Dockerfile without committing to a full run.

## Considered Options

- **Status quo (user must remember to `pycastle build`).** Rejected: this is exactly the foot-gun the change is meant to remove. The cost of forgetting is one wasted agent run on a stale image, paid in tokens and wall time.
- **Existence check only (skip rebuild if image exists).** Rejected: solves "image missing on fresh clone" but not the actual reported pain — image exists *but is stale*. A bare existence check would still let edits to `Dockerfile` / `.python-version` go un-applied.
- **Hash the build context ourselves and rebuild on mismatch.** Rejected: reimplements Docker's cache-invalidation logic in user space. Easy to omit a file the `Dockerfile` `COPY`s in and silently regress to the original problem. Docker's own layer cache is the source of truth for "did anything change"; deferring to it is both simpler and harder to get wrong.
- **Always invoke `docker build` and rely on the layer cache (chosen).** When nothing changed every step is `CACHED` and the daemon roundtrip is the only cost. When something did change, Docker rebuilds exactly the affected layers.
- **Verify inside `orchestrator.run` or `ContainerRunner.work` instead of the CLI command.** Rejected: pulls a `DockerService` dependency into the orchestrator (currently it goes through `ContainerRunner`) and tangles build output with the live iteration status display. The verify is a property of the *CLI contract* for `run`, not an orchestrator invariant, and the call site is naturally a single one-liner in `run_cmd`.
- **Add `--no-build` opt-out and/or `--no-cache` on `run`.** Rejected on both counts. `--no-build` invites users to "just always pass it" and recreates the stale-image problem. `--no-cache` belongs on the explicit `build` command; the rare full-rebuild workflow (`pycastle build --no-cache && pycastle run`) stays clear without a shortcut.

## Consequences

- `pycastle run` calls the same build path as `pycastle build` before loading containers; failures abort `run` with the build error and no fallback to a possibly-stale image.
- On the cache-hit path the build output is suppressed; a one-liner (e.g. `Image up to date`) confirms the verify ran. On the cache-miss path build progress streams live, surfaced via a status row, because layer rebuilds can take minutes and silent hangs are unacceptable.
- `pycastle build` continues to exist unchanged. It owns `--no-cache` and the standalone "did my Dockerfile break" check; `run` does not expose `--no-cache`.
- `build_command.main()` must stop calling `sys.exit` on success so it can be invoked in-process from `run_cmd`. Exit semantics move to the CLI entry points.
- Docker daemon availability becomes a precondition of every `run` (it already was, in practice — `pycastle run` cannot start containers without it). No new fallback is introduced.
