# Managed-worktree mount invariants and fallback issue contract

Managed worktrees now carry more than checked-out source: they anchor `.pycastle-session/`, provider-owned resume state, failure artifacts, and branch-local recovery behavior. That means role reruns and diagnostics cannot treat "same branch, different checkout" as equivalent when they need preserved in-flight state or failure evidence.

The remaining ambiguity was what to do when the expected managed worktree path is missing or cannot be mounted. Role runs, diagnostic agents, and branch-only in-flight recovery all need one documented contract so recovery behavior stays predictable and fallback issues stay correctly classified.

## Decision

- The managed-worktree path is a runtime invariant for any role or diagnostic run that depends on preserved session state, provider resume state, or failure evidence. The path mounted into the container at `/home/agent/workspace` must be the managed worktree path itself, not an arbitrary checkout of the same branch.
- Branch-only in-flight recovery is allowed only for managed branched worktrees. When the canonical issue or sandbox branch still exists but the managed worktree directory is gone, pycastle may recreate that managed worktree from the branch and resume there. This recreates the canonical managed path before dispatch; it does not broaden resume to detached or ad hoc checkouts.
- Detached transient diagnostics remain valid only for branch-independent work. They must not impersonate a missing managed worktree when the diagnostic needs `.pycastle-session/`, provider state, or preserved failure artifacts from a managed path.
- If pycastle cannot prepare the required managed mount path for a diagnostic fallback, it does not launch a degraded diagnostic agent. Instead it files the fallback issue directly.
- Those fallback issues carry labels `bug` + `needs-triage` only. They must explicitly state that no diagnostic agent ran because the managed worktree mount path could not be prepared.
- This fallback route is documentation and behavior-policy only. It does not add a new public configuration surface, and it does not change the existing AFK/HITL issue-label policy for successful diagnostic-agent runs.

## Consequences

- Managed worktrees are the sole resumable mount source for role reruns and for diagnostics that depend on preserved agent state.
- Recreating a managed worktree from an existing branch is an in-flight recovery tool, not a general permission to treat branches and worktree paths as interchangeable.
- Fallback issues caused by mount preparation failure stay clearly operator-actionable and non-AFK: `bug` + `needs-triage`, with explicit wording that no diagnostic agent ran.
- Successful diagnostic-agent runs keep their existing issue-filing policy; only the mount-failure fallback path bypasses the diagnostic agent entirely.

## Related

- Host check loop for current-OS diagnostics: ADR 0036
- Runtime compatibility artifact policy: ADR 0045
