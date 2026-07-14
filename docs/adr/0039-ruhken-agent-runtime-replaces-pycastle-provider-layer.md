# ruhken-agent-runtime replaces pycastle provider layer

> **Supersedes** (these ADRs are retired; their decisions are subsumed here): session resume via derived UUID, the pluggable `AgentService` streaming-execution seam, three-bucket Claude API error handling, per-agent CLI flag profiles, CLI version pins + Codex sandbox policy, service-aware Failure-Report session dir, and shared credential-failure routing. The one exception is the OpenCode idle-timeout-to-usage-limit routing, which this ADR wrongly claimed to supersede — it is restored under ar by ADR 0042.

Pycastle's own provider execution layer — `AgentService`, `ClaudeService`, `CodexService`, `OpenCodeService`, `flag_profiles.py`, `parsed_event_reducer.py`, `session/resume.py`, `session/provider_session_state.py`, `session/provider_run_state.py`, `session/service_resume_identity.py`, `infrastructure/_logged_line_stream.py`, and the `work.py` abstraction layer (`invoke_work` and associated protocols) — is replaced by `ruhken-agent-runtime` (`ar`, package `agent_runtime`).

Ar's `RuntimeClient` provides three execution patterns (`run_ephemeral`, `run_new_session`, `run_resumed_session`) returning a closed `RuntimeOutcome` set. Ar retains subprocess execution ownership — stdin, idle timeout, exit codes, provider CLI version management. Pycastle retains orchestration, config, GitHub, Docker lifecycle, worktrees, prompts, and XML output parsing.

## Key decisions

**Session state and resume.** Ar's `Continuation` token (opaque serialized pointer carrying provider resume state, service, model, effort, and tool access) replaces pycastle's derived-UUID session IDs, `--resume`/`--session-id` flags, `CLAUDE_CONFIG_DIR`, and per-service transcript files. Pycastle persists `Continuation.serialized` to a `_continuation` file inside `RoleSession.path` after every interrupted run (this file lives one level above `session_store`; see the amendment at the end of this ADR). `is_resumable()` detects this file; `clear_provider_state_and_signal_completion()` clears it. Fresh vs. Resume dispatch is based on `_continuation` file presence alone.

**Error routing.** Ar surfaces typed exceptions (`HardAgentError`, `AgentCredentialFailureError`) and outcome kinds instead of pycastle's per-service stream-parsing rules. Pycastle routes ar outcomes: `UsageLimited` and `ProviderUnavailable(SERVICE_NOT_AVAILABLE)` → `TemporaryUsageLimit`; `AgentCredentialFailureError` → `PermanentlyExhausted(reason="credential_failure")`; `ProviderUnavailable(TRANSIENT_API_ERROR)` → `TransientAgentError`; `TimedOut` → resume via `run_resumed_session` + RESUME prompt up to `timeout_retries`; `ar.HardAgentError` → `pycastle.HardAgentError` (status_code and observations dropped).

**Tool policy.** Ar's `ToolPolicy` enum (`NONE`, `NO_FILE_MUTATION`, `UNRESTRICTED`) replaces `flag_profiles.py`. Restricted roles (Planner, Divergence-Resolver) use `NO_FILE_MUTATION`; all other roles use `UNRESTRICTED`. Per-service CLI flag matrices are deleted; ar manages provider-specific flag construction internally.

**Usage limit types.** `UsageLimitOutcome(is_permanent: bool)` is replaced by two explicit types: `TemporaryUsageLimit(reset_time: datetime | None)` (sleep and retry) and `PermanentlyExhausted(reason: str)` (rotate credential/service). `decide_usage_limit_continuation()` becomes a type dispatch on these two types; the `is_permanent` flag is removed.

**`AgentFailedError`.** Stays in pycastle — raised by pycastle on output-protocol failure after reprompt exhaustion, not by ar. The computed `session_dir` property (which reconstructed the path from `RoleSession` + service name) is replaced by a direct `Path` field set by the raising call site to the `session_store` path. `provider_errors.ProviderErrorObservation` is dropped; the invocation log covers raw output evidence.

**`model` config field.** Empty string (`model=""`) is forbidden at config load time and raises `ConfigValidationError`. Every `StageOverride` must name a model explicitly. The `config.py.example` scaffold already lists explicit models for every stage.

**Work invocation layer.** `invoke_work`, `WorkInvocationRequest`, `WorkOutputAdapter`, `PrepareSessionAdapter`, and related protocols are deleted. Iteration phases call `RuntimeClient` directly.

## Considered options

- **Keep `AgentService` abstraction, add an ar adapter behind it.** Rejected: the `invoke_work` / `PreparedSession` / `WorkOutputAdapter` layer exists to decouple iteration from `ContainerRunner` + `AgentService`; ar makes this seam redundant. Two execution models would coexist, doubling the surface to maintain.
- **Wrap ar in a pycastle-shaped adapter.** Rejected: adapting ar to the old interface defeats the point of adopting ar as the shared execution boundary.
- **Keep `model=""` CLI-default escape hatch.** Rejected: ar's `ProviderSelection` requires non-empty model; the escape hatch was a convenience shortcut that now becomes a silent misconfiguration path.

## Consequences

**Auth seeding.** `prepare_local_provider_run_state()` (which called `auth_seed_action.apply()`) is deleted alongside the old provider layer, but the seeding step itself must survive. In the ar path, `_run_with_runtime_client` in `AgentRunner` calls `service.provider_session_state()` with the service-specific `provider_state_dir` (derived from `service.state_dir_relpath()`, not from `role_session.path`) and applies `auth_seed_action` before handing control to ar. This preserves the invariant from ADR 0017: `CODEX_HOME/auth.json` is seeded from `~/.codex/auth.json` before the container starts.

- `services/claude_service.py`, `codex_service.py`, `opencode_service.py`, `agent_service.py`, `flag_profiles.py`, `session/provider_run_state.py`, `provider_session_state.py`, `service_resume_identity.py`, `resume.py`, `parsed_event_reducer.py`, `infrastructure/_logged_line_stream.py`, `provider_errors.py`, and the `work.py` abstraction layer are deleted.
- `RoleSession` retains lifecycle methods (`path`, `start_fresh`, `clear_provider_state_and_signal_completion`, `discard`, `is_resumable`, `is_done`) and adds `_continuation` file read/write; all provider-specific methods are deleted.
- `AgentFailedError` drops the `session_dir` computed property; gains a direct `Path` field for `session_store`.
- `decide_usage_limit_continuation()` signature changes from `outcome: UsageLimitOutcome` to `TemporaryUsageLimit | PermanentlyExhausted`.
- All resumable ar runs require a `session_store` path; pycastle passes the per-provider state dir (see the amendment at the end of this ADR — originally `RoleSession.path`).
- Protocol reprompt on `AgentOutputProtocolError` is implemented as a `run_resumed_session` call with the reprompt text and the `Continuation` from the previous `Completed` run.
- `ContainerRunner` drops all provider-CLI invocation logic; see ADR 0040.

## Amendment (#1954): `session_store` is the per-provider state dir

The original decision passed `RoleSession.path` (`.pycastle-session/<role>[/<namespace>]/`) as `session_store`. That worked with the ar version current at the time, which nested provider state further under `session_store` before probing it. Ar ≥ 2.4 changed the contract: with a caller-owned `session_store` it probes that directory **directly** for the provider transcript, adding no owner/provider segment of its own.

Because pycastle sets the provider's own config dir (`CLAUDE_CONFIG_DIR`/`CODEX_HOME`) to the per-provider path `.pycastle-session/<role>[/<namespace>]/<provider>/` (from `service.state_dir_relpath()`, per ADR 0040 argv routing), passing the bare `RoleSession.path` left ar probing a directory the provider never writes to. On resume ar found no transcript, downgraded `RunKind.RESUME → FRESH`, reused the continuation's session id, and Claude aborted with *"Session ID … is already in use."*

**Correction:** `AgentRunner._run_runtime_once` now passes `session_store = provider_state_dir` (the already-computed `request.mount_path / service.state_dir_relpath(...)`, falling back to `RoleSession.path` when a service has no per-provider state dir) to both `run_new_session` and `run_resumed_session`. Ar now probes exactly where the provider reads/writes.

Consequence for layout: `session_store` is now **nested under** `RoleSession.path`. Pycastle's own `_continuation` and `_done` sentinel files remain at `RoleSession.path` (one level up); `is_resumable()`/`is_done()` are unchanged. Session cleanup (`start_fresh`, `discard`, `clear_provider_state_and_signal_completion`) still operates on `RoleSession.path` and cascades into the nested provider dir.
