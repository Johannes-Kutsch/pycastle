# GitHub PAT as the sole GitHub credential

Pycastle previously had two parallel GitHub auth paths: `gh auth login` (browser OAuth via `gh` CLI) and `GH_TOKEN` (direct `urllib` + agent containers). Either could 401 invisibly to the other. All GitHub access now routes through `GithubService` as an HTTP client authenticated via `GH_TOKEN`. One credential, used identically host-side and in containers, validated by a `GET /user` startup preflight.

Note: ADR 0021 partly reverses the "no `gh` install" sub-claim — agent containers do install `gh` for prompt-driven issue ops, but host code still uses PAT + `urllib`.

## Considered Options

- **Dual auth (status quo).** Rejected: silent drift between the two creds produced the original bug. `gh auth status` preflight covers only half.
- **`gh` on host + `GH_TOKEN` in containers.** Rejected: two creds per machine, surprising split, `gh` becomes a hard `pycastle labels` dependency.
- **PAT-only (chosen).** One cred everywhere, honest errors, container/host parity.

## Consequences

- User manages PAT expiration (recommend "no expiration" classic PAT for personal use).
- SSO orgs need one-click PAT authorization via web UI (no automatic SSO flow).
- `GithubService` no longer inherits `_SubprocessService`; owns its own `urllib` `_request` / `_paginate`.
- Exception hierarchy reshapes around HTTP: `GithubAuthError` (401), `GithubAPIError` (other non-2xx), `GithubNetworkError` (transport). Subprocess-shaped `GithubCommandError` / `GithubTimeoutError` / `GithubNotFoundError` removed.
