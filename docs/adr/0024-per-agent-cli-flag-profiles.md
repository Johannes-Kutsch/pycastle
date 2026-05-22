# Per-agent Claude CLI flag profiles for token reduction

`claude_service.build_command` previously emitted the same flag set for every agent role. The orchestrator now selects a per-role flag profile that strips Claude Code surface the role doesn't use, plus a small set of universal token-savers. The profile is hardcoded per `AgentRole` — not user-tunable — because the flags encode the role's *contract* (Planner emits JSON; Reviewer enforces conventions; divergence-resolver is mechanical) and not a tuning preference.

## Universal flags (every role)

- `--disable-slash-commands` — pycastle agents never invoke slash commands; user-authored skills/commands in the mounted worktree are silent prompt-injection vectors outside the prompt contract.
- `--exclude-dynamic-system-prompt-sections` — hoists per-machine sections (cwd, env, memory paths) to the first user message so the system prompt is cache-stable across worktrees; a structural win for parallel implement runs on `.pycastle/.worktrees/issue-<N>-<slug>`.

## Bare set: Planner + divergence-resolver

Both run `--bare`, which skips auto-discovery of hooks, skills, plugins, MCP, auto memory, and CLAUDE.md and restricts built-in tools to Bash + Read + Edit.

- **Planner** — emits `<plan>` JSON over injected issue lists; doesn't grep the codebase. Bare safe; further restricted with `--tools "Read,Glob"` since Bash and Edit are unused. CLAUDE.md auto-discovery skipped, but the plan prompt is updated to instruct the agent to consult `CONTEXT.md` and `docs/adr/` directly via `Read` when blocker analysis needs architectural context.
- **Divergence-resolver** — bare safe *only after* the no-CHECKS contract change below.

## Non-bare investigator restriction

`preflight-issue` and `improve scan` both file GitHub issues via `gh` over Bash and never edit code in the worktree. Both get `--disallowedTools "Edit Write NotebookEdit"` — enforces the read-only contract at the tool layer instead of by prompt convention only.

## Non-bare full-tool roles

Implementer, Reviewer, Merger get no tool restriction. Reviewer is in this group despite emitting `<commit_message>` because `review-prompt.md` instructs the reviewer to write missing tests, refactor red-flag tests, fix bugs found, and reduce complexity (steps 2-6) — i.e. Reviewer actively modifies code.

## MCP suppression on non-bare roles

All five non-bare roles also get `--strict-mcp-config --mcp-config '{}'` — same logic as `--disable-slash-commands`: MCP servers from the mounted worktree's `.mcp.json` are silent tool-surface expansions outside the prompt contract.

## Divergence-resolver no-CHECKS contract

Coupled change without which divergence-resolver is *not* bare-safe.

Previous: `diverge-prompt.md` step 5 instructed the resolver to run `{{CHECKS}}` post-merge and step 6 committed unconditionally. Prompt was silent on what to do if CHECKS failed, giving the agent latitude to fix code inline — latitude that relied on CLAUDE.md and conventions context.

New: divergence-resolver does textual conflict resolution only. Prompt explicitly instructs the agent **not** to run CHECKS. `<promise>COMPLETE</promise>` iff the merge commits cleanly; `<promise>FAILED</promise>` iff conflicts cannot be resolved textually. Post-merge breakage is detected by the same `get_safe_sha()` call (`iteration/preflight.py:207-258`) — after `pull_with_resolution` returns and main is fast-forwarded, control re-enters `get_safe_sha` which reads the new HEAD, sees cache miss, runs `PREFLIGHT_CHECKS` in `preflight-sandbox`, and files an AFK issue routed through the existing preflight-fix path.

Trade-off: main briefly carries a potentially broken merge until preflight-fix lands a repair. Acceptable because (a) main is already allowed to be red between iterations by pycastle's design, (b) `PreflightCache` holds the broken-HEAD verdict so the next iteration's first `get_safe_sha` call observes it (no duplicate filings), and (c) the previous design produced the same broken-main window if the resolver's inline fix-up missed a regression checks didn't catch.

## Considered Options

- **Hardcode flags inline in `build_command` without a per-role parameter.** Rejected: would force `build_command` to know about every role anyway via conditional branches; cleaner to thread `role` through and look up the profile.
- **Expose flag profiles in `STAGE_OVERRIDES` so consuming projects can override them.** Rejected: model + effort are tuning knobs; tool/permission surface is contract. A user flipping `--bare` off on Planner would silently re-introduce CLAUDE.md token load with no value; a user flipping `--disallowedTools` off on Reviewer would break the "Reviewer is read-only" assertion the matrix relies on. Per-role contracts belong in code, not config.
- **`--bare` for Reviewer / preflight-issue too.** Rejected: Reviewer edits code and enforces project conventions documented in CLAUDE.md; preflight-issue's value comes from the contextualised bug body it writes, which degrades without CLAUDE.md.
- **`--bare` for Merger.** Rejected: Merger handles conflict resolution between agent-authored branches at the highest-stakes integration step. Stripping conventions context here risks bad merges that escape the merge-sandbox preflight gate.
- **Leave divergence-resolver running CHECKS but lock its prompt to `FAILED` on check failure.** Rejected in favour of the no-CHECKS path: cleaner separation (resolver does textual merge only, preflight-issue agent does diagnosis, Implementer does repair). Avoids the resolver doubling as a code-repair agent for a job preflight-issue is already shaped to handle.
- **`--tools ""` (zero tools) for Planner.** Rejected after verifying that the plan prompt's blocker rules benefit from selective reads of `CONTEXT.md` and `docs/adr/`. `--tools "Read,Glob"` is the minimal viable set; the plan prompt addition makes the read intent explicit.

## Consequences

- `claude_service.build_command` gains a `role: AgentRole` parameter; per-role flag lookup lives in the same module.
- `diverge-prompt.md` step 5 is rewritten to *forbid* running `{{CHECKS}}`; `{{CHECKS}}` placeholder is dropped from the prompt's scope.
- `plan-prompt.md` gains one line instructing the Planner to `Read` `CONTEXT.md` and `docs/adr/` selectively when blocker analysis needs architectural context.
- Token savings are largest for Planner (skips CLAUDE.md + MCP + skills + plugins + hooks; restricts tool defs to two) and divergence-resolver (same, plus the prompt is shorter). Other roles save the slash-commands + MCP tool defs + investigator roles also save Edit/Write/NotebookEdit defs.
- Anthropic prompt-cache hit rate improves across parallel Implementer/Reviewer runs because `--exclude-dynamic-system-prompt-sections` removes the per-worktree path from the system prompt.
- Post-Merger broken-main detection when the Merger closes the last AFK issue is **not** covered by this ADR — orchestrator currently exits the loop without a final `get_safe_sha` call in that case. Tracked separately (issue handed off during grilling).
