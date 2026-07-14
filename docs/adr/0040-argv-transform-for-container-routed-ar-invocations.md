# argv_transform for container-routed ar invocations

Ar provides `argv_transform: Callable[[tuple[str, ...], Path, Mapping[str, str]], tuple[str, ...]] | None` on all three request objects (`EphemeralRunRequest`, `NewSessionRunRequest`, `ResumedSessionRunRequest`). When present, ar calls the transform with the fully-built provider CLI argv, the working directory path, and the fully-rendered environment (credentials + ar-generated values such as `OPENCODE_CONFIG_CONTENT`), then executes the returned argv instead. Ar retains subprocess execution ownership; pycastle provides only the transform.

Pycastle uses this to route provider invocations into a Docker container: the transform prepends `docker exec -i <container_id>` and injects required env vars via `-e KEY=VALUE` flags. The `-i` flag is required so that ar's stdin pipe (carrying the prompt) is forwarded into the container process; without it docker exec disconnects stdin and providers that read their prompt from stdin (Codex) exit immediately. `ContainerRunner` becomes a thin Docker lifecycle wrapper — start container → supply `argv_transform` closure over `container_id` → tear down.

## Behaviors ar applies when argv_transform is present

- Stdin prompt transport is forced (no host-side prompt file needed).
- `--sandbox danger-full-access` is automatically applied for Codex invocations.
- Transform is applied before host executable resolution (no Windows `.cmd` paths leak into the docker exec argv).
- Per-request, not client-level — one `RuntimeClient` can mix container-routed and host-local invocations.

## Considered options

- **Ar natively supports Docker.** Rejected: ar must remain Docker-free to serve consumers that do not use Docker.
- **Pycastle owns subprocess execution (`command_runner` pattern).** Rejected: reimplementing stdin, idle timeout, exit codes, and partial-line buffering in pycastle duplicates ar's execution machinery and creates two surfaces to maintain.
- **Forward all env vars into the container.** Forwarding all is safe but includes host-specific noise (`PATH`, virtualenv markers, etc.). Only credentials and ar-generated vars (notably `OPENCODE_CONFIG_CONTENT`, which ar generates internally and pycastle cannot reconstruct) must be forwarded; additional filtering is left to the implementation.

## Consequences

- `ContainerRunner` retains Docker container lifecycle only (start, stop, git identity propagation via `exec_simple`); all provider-CLI invocation logic is removed.
- The `already_sandboxed` field on `RuntimeClient` constructor is removed; sandbox behavior is implied by transform presence per invocation.
- Provider-specific `build_command` implementations and `flag_profiles.py` are deleted; ar builds the provider CLI argv internally.
