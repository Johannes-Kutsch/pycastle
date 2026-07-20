# Use ar error types directly; remove pycastle's translated equivalents

Issue #1960 revealed a translation-gap class of bug: `agent_runtime` can raise `agent_runtime.errors.TransientAgentError` directly as an exception (not only as a `ProviderUnavailable` outcome kind), and pycastle's iteration boundary handlers only caught `pycastle.errors.TransientAgentError` — a completely unrelated class — so the error escaped and crashed the run. The fix for #1960 patched the gap by catching `RuntimeTransientAgentError` in `_invoke_runtime_attempts`, but the underlying problem is that a translation layer with N error types has N potential gaps.

The decision is to eliminate the translation layer entirely. `pycastle.errors.TransientAgentError`, `pycastle.errors.HardAgentError`, and `pycastle.errors.AgentCredentialFailureError` are deleted; all catch blocks, isinstance checks, type annotations, and raise sites throughout pycastle use `agent_runtime.errors.*` directly. `pycastle.errors.AgentCredentialFailureError` is a subclass of `pycastle.errors.HardAgentError`; the ar hierarchy mirrors this exactly. The coupling to ar's type hierarchy is accepted — both packages share a maintainer, and translation-gap bugs like #1960 become structurally impossible.

## Considered options

- **Keep the translation layer, patch each gap as found.** Rejected: #1960 is the second translation-gap bug (the first was `RuntimeContinuationUnrecoverableError`). Each new ar exception type is a future gap. Patching on discovery is slower than removing the layer.
- **Add `status_code` to ar's `AgentCredentialFailureError` to preserve pycastle's fallback routing.** Not needed: investigation confirmed that ar never propagates `status_code` into `AgentCredentialFailureError` (it is dropped at the `CredentialFailure` event → exception conversion step). The `if status_code == 403 / 401` branches in `agent_credential_failure_routing.py` are already dead for all ar-originated errors; routing works via `classification` and text matching instead.

## Consequences

- `pycastle.errors` retains `AgentFailedError`, `AgentTimeoutError`, `UsageLimitError`, `ModelNotAvailableError`, `DockerError`, `SetupPhaseError`, and `OperatorActionableGitError` — these have no ar equivalents.
- The translation except-blocks in `AgentRunner._invoke_runtime_attempts` (`except RuntimeHardAgentError`, `except RuntimeAgentCredentialFailureError`, `except RuntimeTransientAgentError`) are deleted; `_invoke_runtime_attempts` catches ar types directly.
- `agent_credential_failure_routing.py`, `worktree.py`, `implement.py`, `_merge_conflict_recovery.py`, `iteration/__init__.py`, and `runtime.py` update their imports and annotations from `pycastle.errors` to `agent_runtime.errors` for the three deleted types.
- The dead `if service_name == "claude" and status_code == 403:` and `if service_name != "codex" or status_code != 401:` branches in `_interpret_agent_credential_failure` are removed.
