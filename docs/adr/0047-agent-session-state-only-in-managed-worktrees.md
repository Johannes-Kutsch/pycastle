# Managed session and diagnostic workspaces

## Decision

Agent-provider session state must be kept only inside managed worktrees under `pycastle/.worktrees/`, never in the main checkout root. Pycastle shall ignore root-level `.pycastle-session` and reject non-managed mounts before provider setup for both role runs and preflight-style work (`run_preflight`, host-check execution/reporting, and related diagnostic entry points). This prevents synced root-level artifacts such as Codex `auth.json` from poisoning provider auth lineages across devices.

## Consequence

When a diagnostic path cannot run because the expected managed worktree is missing or invalid, pycastle must not attempt to resume or run diagnostic agent work there. Instead, it should skip the diagnostic agent step and file or reuse a minimal direct issue in the consuming project:

- labels `bug` and `needs-triage` only
- include failed role and expected worktree path
- include the rejection reason and original failure summary
- include explicit `no agent diagnosis ran`

This fallback path is used for Failure-Report and host-check diagnostic flows when preconditions are not met.

## Design context

The same managed-worktree path contract applies to production iteration preflight, check-mode host preflight, and any diagnostic run that relies on durable session artifacts.

