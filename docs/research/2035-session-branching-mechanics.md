# Research: Session branching mechanics for N spec→slice agents

Resolves [Research: Session branching mechanics for N spec→slice agents](https://github.com/Johannes-Kutsch/pycastle/issues/2035) (child of [Map: Host-owned issue filing (to-spec + to-tickets)](https://github.com/Johannes-Kutsch/pycastle/issues/2034)).

## Question

For the N-candidate sequential improve flow, each spec→slice agent must resume the scan session transcript at a branching point, with its own isolated session storage so candidate B's context is not flooded by candidate A. How is that implemented per provider (Claude, Codex, OpenCode), and how does it fit pycastle's existing session machinery?

## Headline finding

**Every resumable pycastle session is a self-contained file tree** rooted at `RoleSession.path` =
`<worktree>/.pycastle-session/<role>/[<namespace>/]`. There is **no host-unreachable server-side session state** and **no SQLite/lock file** in that tree. So a session can be forked by a plain **recursive directory copy** into a new `RoleSession` identity, and then resumed through the ordinary Resume path. The natural fork axis is the **namespace** — the improve flow already partitions on it (`namespace="main"`).

## What lives in a role session dir

Sentinels sit at the top level; the provider's own state dir (`session_store`) is nested one level down (`CLAUDE_CONFIG_DIR`/`CODEX_HOME`/opencode data dir are bind-mounted onto it):

```
<worktree>/.pycastle-session/<role>/[<namespace>/]
├── _session_uuid_seed      # seed for the deterministic Claude session uuid (runtime_session.session_uuid)
├── _continuation           # Continuation.serialized — the resume token (ADR 0039)
├── _done                   # completion sentinel (is_done)
├── _fingerprint            # context fingerprint (ADR 0050)
├── _phase_progress / _phase_in_flight   # ImprovePhaseDriver state
└── <provider>/             # == session_store; ar reads/writes here (#1954)
```

`RoleSession` (`src/pycastle/session/role.py`) already owns `start_fresh()` (rmtree + recreate) and `discard()` (rmtree) over this tree — a `fork` operation is the missing sibling (a `copytree` into a new identity).

## Per-provider resume identity

| Provider | Native local state (under `session_store`) | How the resume id is recovered | Server-side state? | Lock/DB? |
|---|---|---|---|---|
| **Claude** | Transcript `.jsonl` keyed by a **session uuid** under the Claude config dir | `session_uuid()` = **deterministic** `uuid5(role, namespace, worktree.resolve(), _session_uuid_seed)`; on Resume the copied `_continuation` supplies the concrete id | No — stateless API, transcript replayed from local files | No |
| **Codex** | `sessions/rollout-*.jsonl` under `CODEX_HOME` | `thread_id` scanned out of the single `thread.started` event in the rollout jsonl (`_recover_codex_rollout_thread_id`) | No | No |
| **OpenCode** | opencode session state + a `session_id` file pycastle writes into the state dir | `load_state_dir_provider_session_id()` reads the `session_id` file | No | No |

Verified by `grep`: the only lock in the codebase is the unrelated **project run marker** (`run_lock.py`, an `fcntl` flock on `*.lock` markers in pycastle home — not in session dirs); no `.db`/SQLite anywhere in provider state. (Residual: worth a runtime spot-check that the installed OpenCode version still keeps file-based state rather than a shared DB, since pycastle's contract only reads a `session_id` file.)

## Can the host copy to fork? Yes — with one Claude caveat

Because Codex and OpenCode embed their resume id **inside the copied files** (rollout jsonl / `session_id`), a plain copy is already self-consistent.

Claude's id is **derived from the path** (`worktree.resolve()` + role + namespace + seed). Copying the session into a *different* namespace or worktree makes `session_uuid()` recompute a **different** uuid that no longer matches the copied transcript filenames. Resolution: **the fork carries the `_continuation` file**, and the Resume path prefers the continuation token (which holds the concrete session id) over re-deriving from the path. Path-derived uuid is only the fallback when neither a continuation nor a preferred id exists. So: **copy `_continuation` together with the provider dir and Claude resumes correctly regardless of the new location.**

## Recommended branching approach

**Fork on the namespace axis, inside the existing improve-sandbox worktree.**

- Keep the shared scan under `.pycastle-session/improve/main/`. At **scan completion** (after the scan agent emits the N ordered candidates, before any PRD phase), `copytree` the `main` session into per-candidate namespaces `.pycastle-session/improve/candidate-<k>/` — same worktree, same `AgentRole.IMPROVE`, distinct namespace.
- Each candidate's PRD→issues phases run under its own namespace, **resumed from its forked `_continuation`**, so candidate B's PRD agent resumes the *scan* transcript and never sees A's PRD/issues context. This directly satisfies the isolation requirement.
- **Reset `_phase_progress`/`_phase_in_flight` in each fork** to the branch point, so a candidate doesn't inherit another's phase-03 marker.

Why namespace over per-candidate worktrees: namespaces are subdirs of one worktree, so they inherit the improve-sandbox's existing preservation/teardown and **create no new orphan-sweep class**. Forking into separate worktrees would make each an `orphan sweep` subject requiring its own git registration and teardown — extra machinery for no isolation gain (the namespace copy is already fully isolated), and it *worsens* the Claude path-derivation issue.

## Lifecycle

- **Fork**: at scan completion, `main` → `candidate-1..N` (eagerly, or lazily per candidate from a preserved `main` snapshot). Must happen **before** `clear_provider_state_and_signal_completion()` runs on `main`, since that purges provider state.
- **Run**: resume each candidate from its namespace; PRD → issues.
- **Clean finish**: `discard()` (or `clear_provider_state_and_signal_completion()`) that candidate's namespace.
- **Error/partial**: `discard()` the failed candidate's namespace; the improve-sandbox worktree and remaining candidate namespaces persist for retry/diagnosis under existing preservation rules.
- **Cleanup**: when all candidates finish, the improve-sandbox worktree tears down normally (`delete_branch_on_teardown=True`), taking every candidate namespace with it. **Orphan sweep needs no change** — no new worktrees, and `any_role_dir_present()` already sees `.pycastle-session/improve/` as present while candidates are live.

## Precedent mapping

- `RoleSession(worktree, role, namespace)` — the identity to duplicate; add a `fork_to(new_namespace)` helper alongside `start_fresh()`/`discard()` that `copytree`s `path`, carrying `_continuation`, `_session_uuid_seed`, `_fingerprint`, and the provider dir, then resets phase markers.
- `discard()` — the per-candidate teardown, already available.
- `clear_provider_state_and_signal_completion()` — preserves service-session metadata sidecars (`is_service_session_metadata_path`); fork must snapshot the source *before* this runs.
- `_PHASES` registry (`improve.py`) — today every phase is `namespace="main"`; the N-candidate flow parametrizes the namespace per candidate (scan stays `main`; each candidate's PRD/issues run under `candidate-<k>`).
- `ImprovePhaseDriver` — its `_phase_progress`/`_phase_in_flight` files ride along in the copy and must be reset per fork.
- `orphan sweep` / `any_role_dir_present` — unchanged under the namespace approach.

## Open follow-ups (for later tickets, not this one)

- Runtime spot-check of the installed OpenCode session-storage format (file vs. DB).
- Exact fork mechanics — eager copy of all N at scan completion vs. lazy copy-on-start from a preserved `main` snapshot — belongs to the ImprovePhaseDriver redesign ticket (#2039).
