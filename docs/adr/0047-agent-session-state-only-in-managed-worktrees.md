# Managed session and diagnostic workspaces

## Decision

Agent-provider session state must be kept only inside managed worktrees under `pycastle/.worktrees/`, never in the main checkout root. Pycastle shall ignore root-level `.pycastle-session` and reject non-managed mounts before provider setup for both role runs and preflight-style work (`run_preflight`, host-check execution/reporting, and related diagnostic entry points). This prevents synced root-level artifacts such as Codex `auth.json` from poisoning provider auth lineages across devices.

Managed worktrees are the canonical source of truth for role reruns and diagnostics that depend on `.pycastle-session`, provider state, failure artifacts, or branch-local recovery. The path mounted into the container at `/home/agent/workspace` must be that managed worktree path, not an arbitrary checkout of the same branch, because preserved provider state and diagnostics artifacts are path-local contracts.

Branch-only in-flight recovery is allowed only when the managed branched worktree can be recreated from its branch:

- If the canonical issue or sandbox branch still exists but the managed worktree directory is gone, recreate that managed branched worktree from the branch and resume there.
- Detached transients are valid only for branch-independent work. They do not substitute for missing managed paths when the mount invariants require preserved in-flight context.
- Recreating a managed worktree from an existing branch is a recovery path for in-flight work only; it does not authorize treating detached checkouts or ad hoc checkouts as interchangeable with managed worktrees.

## Consequence

When a diagnostic path cannot run because the expected managed worktree is missing or invalid, pycastle must not resume in a degraded checkout. Instead, it skips the diagnostic agent and files or reuses a minimal direct issue on the consuming project with labels `bug` + `needs-triage` only:

- failed role and expected worktree path
- rejection reason and original failure summary
- explicit `no agent diagnosis ran`

This fallback route is used for Failure-Report and host-check diagnostic flows when preconditions are not met.

## Design context

This contract applies to production iteration preflight, check-mode host preflight, and any diagnostic run that relies on durable session artifacts. It also explains the branch-only in-flight recovery path above.

## Related

- Host check loop for current-OS diagnostics: ADR 0036
- Runtime compatibility artifact policy: ADR 0045
