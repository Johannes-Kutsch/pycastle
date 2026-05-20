# Recently-closed filter on `GithubService.get_open_issues`

GitHub's issue *list* endpoint is eventually consistent with `PATCH /issues/{n}` — for a brief window after a close, the list can still include the just-closed issue. This caused iteration N+1's Plan agent to re-pick issues #558 / #559 seconds after iteration N closed them.

Absorbed inside `GithubService`: every successful `close_issue(n)` adds `n` to private `_recently_closed: set[int]`; `get_open_issues(label)` filters the API response through that set. **Self-healing** — on every call, any number in the set not present in the API response is dropped (consistency caught up).

## Considered Options

- **No client-side guard (status quo).** Rejected: small but reproducible race; makes the log lie about what work was picked.
- **Settle delay between Merge and Preflight.** Rejected: probabilistic; leaks API quirk into orchestrator timing.
- **Verify-then-proceed in Merge (poll single-issue endpoint).** Rejected: single-issue endpoint is consistent, but *list* is served from a separate cache and still lags.
- **Re-verify each candidate in Preflight (single-issue per result).** Rejected: N extra requests for an edge case; scatters workaround outside the adapter.
- **Track recently-closed on the iteration loop / `Deps`.** Rejected: race is an adapter artefact, not orchestrator domain logic.
- **TTL-based eviction.** Rejected: no principled TTL; self-healing uses GitHub's own response as the consistency signal.
- **Filter every list-style read.** Rejected: only `get_open_issues` is the planning path. Self-healing requires observing each call's response; a method not called between Merge and Plan can't self-heal.

## Consequences

- `GithubService` gains private `_recently_closed: set[int]`, init empty.
- `close_issue(n)` adds `n` after PATCH returns successfully. On `GithubAPIError` / `NetworkError` / `AuthError` the set is untouched.
- `get_open_issues(label)`: call list endpoint → build response number set → for each `n` in `_recently_closed`, discard `n` if not in response → filter response by `_recently_closed`. Discard uses the *raw* response so a just-closed issue still present stays filtered, while one that has fallen out is forgotten.
- Self-healing naturally bounds the set. Unbounded growth requires an unbounded close stream with no `get_open_issues` calls — orchestrator never produces this.
- No new params on iteration loop, `merge_phase`, `preflight_phase`, `planning_phase`, `_close_issues_parallel`.
- Concurrency: `set.add` is GIL-atomic in CPython; service is single-threaded; no lock. Changes if `GithubService` ever shared across threads.
- Tests use service-level pattern, never the network, never inspect `_recently_closed` directly.
