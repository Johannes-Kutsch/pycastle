# GitHub PAT as the sole GitHub credential

Pycastle previously used two parallel GitHub authentication systems: `gh auth login` (browser OAuth, used by `orchestrator.py:_get_repo` and `GithubService` via the `gh` CLI) and `GH_TOKEN` from `.env` (used by `labels.py` via direct `urllib` calls to `api.github.com`, and propagated into agent containers). Either could fail independently — a 401 in one system was invisible in the other, with no diagnostic surfacing in the orchestrator's generic `RuntimeError`. We are removing the `gh` CLI dependency entirely and routing all GitHub access through `GithubService` as an HTTP client authenticated via `GH_TOKEN`. One credential, used identically by host code and agent containers, validated by a `GET /user` startup preflight that fails fast with a legible error.

## Considered Options

- **Status quo (dual auth).** Rejected: silent drift between the two credentials produced the original bug and would keep producing it. Adding a `gh auth status` preflight covers half the surface and leaves the other half (`GH_TOKEN`-based code paths) un-validated.
- **Unify on `gh` (host) + keep `GH_TOKEN` (containers only).** Rejected as a halfway house: the user still maintains two credentials per machine, the host/container split is itself surprising, and `gh` becomes a hard runtime dependency for `pycastle labels` that isn't strictly needed.
- **PAT-only (chosen).** One credential everywhere. Removes the `gh` install requirement. Errors are honest and consistent. Container/host parity means the same token works in both places.

## Consequences

- The user must manage PAT expiration. Recommended: classic PAT with "no expiration" for personal use; calendar-tracked rotation otherwise.
- SSO-protected orgs require manual one-click PAT authorization via the GitHub web UI (`gh`'s automatic SSO flow is no longer available). Acceptable trade-off for a single-developer personal tool.
- `gh` is no longer a runtime dependency. README and Dockerfile install instructions drop the `gh` step.
- `GithubService` ceases to inherit from `_SubprocessService`; it owns its own `urllib`-based `_request` and `_paginate` primitives.
- The exception hierarchy reshapes around HTTP semantics: `GithubAuthError` (401), `GithubAPIError` (other non-2xx), `GithubNetworkError` (transport). The subprocess-shaped `GithubCommandError`, `GithubTimeoutError`, and `GithubNotFoundError` are removed.
