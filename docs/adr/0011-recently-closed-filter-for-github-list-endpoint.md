# Recently-closed filter on `GithubService.get_open_issues`

The orchestrator's Merge stage closes issues via `PATCH /repos/{repo}/issues/{n}` and immediately returns control; the next iteration's Preflight queries `GET /repos/{repo}/issues?state=open&labels=ready-for-agent`. GitHub's issue *list* endpoint is eventually consistent with respect to single-issue PATCHes — for a brief window after a close, the list can still include the just-closed issue. This caused iteration N+1's Plan agent to re-pick issues #558 and #559 seconds after iteration N closed them. We absorb the inconsistency inside `GithubService`: every successful `close_issue(n)` adds `n` to a private `_recently_closed: set[int]`, and `get_open_issues(label)` filters its API response through that set. The set is **self-healing** — on every `get_open_issues` call, any number in the set not present in the API response is dropped (consistency caught up).

## Considered Options

- **Status quo (no client-side guard).** Rejected: the race is small but reproducible and non-deterministic in a way that makes the orchestrator's log lie about what work it picked.
- **Settle delay between Merge and Preflight.** Rejected: probabilistic — no principled value. Also leaks an "API quirk" workaround into orchestrator timing.
- **Verify-then-proceed in Merge** (poll single-issue endpoint until `state=closed`). Rejected: the canonical endpoint is consistent, but the *list* endpoint is served from a separate cache and can still lag.
- **Re-verify each candidate in Preflight** by hitting the consistent single-issue endpoint for every result. Rejected: N extra requests per iteration for an edge case affecting at most one or two issues; scatters the workaround outside the adapter.
- **Track recently-closed numbers on the iteration loop frame or on `Deps`.** Rejected: the race is an artefact of the GitHub adapter, not of orchestrator domain logic. Putting the filter in the loop leaks "this list endpoint lies sometimes" into iteration code.
- **TTL-based eviction from the recently-closed set.** Rejected: no principled TTL value; self-healing uses GitHub's own response as the consistency signal.
- **Filter every list-style read on `GithubService`.** Rejected: filter only `get_open_issues`, which is the path the iteration loop uses to drive planning. Self-healing requires observing each call's response; a method not called between Merge and the next Plan can't self-heal.

## Consequences

- `GithubService` gains a private `_recently_closed: set[int]`, initialised empty in `__init__`.
- `close_issue(number)` adds `number` to `_recently_closed` after the PATCH returns successfully. On `GithubAPIError` / `GithubNetworkError` / `GithubAuthError`, the set is untouched.
- `get_open_issues(label)` logic: (1) call the list endpoint; (2) build the set of issue numbers in the response; (3) for each `n` in `_recently_closed`, if `n` not in response numbers, discard `n` from `_recently_closed` (consistency caught up); (4) filter the response by removing any item whose number is still in `_recently_closed`. The discard step uses the *raw* response so a just-closed issue still in the response stays filtered while one that has fallen out gets forgotten.
- Self-healing naturally bounds the set: after one or two iterations any closed issue either falls out of the list endpoint or is discarded. Unbounded growth requires an unbounded stream of closes with no intervening `get_open_issues` calls, which the orchestrator never produces.
- The iteration loop, `merge_phase`, `preflight_phase`, `planning_phase`, and `_close_issues_parallel` get no new parameters.
- Concurrency: `set.add` is GIL-atomic in CPython and the service is single-threaded; no lock is added. If `GithubService` is ever shared across threads, this changes.
- Test coverage: (a) close → immediate `get_open_issues` returning just-closed issue → it is filtered; (b) close → `get_open_issues` returning it → second call with it absent → set is now empty; (c) close raising `GithubAPIError` → `_recently_closed` unchanged. Tests use the existing service-level test pattern, never reaching for a real network, and never inspecting `_recently_closed` directly.
