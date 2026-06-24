# Per-agent Claude CLI flag profiles for token reduction

> **Amended (#871):** `--bare` removed from all roles (refuses OAuth auth). Planner and divergence-resolver now use explicit `--tools` restrictions instead. `bare` field on `FlagProfile` removed.

Per-role flag profile hardcoded in `claude_service.build_command` — not user-tunable — because flags encode the role's contract, not a tuning preference.

## Flag matrix

**Universal (every role):** `--disable-slash-commands`, `--exclude-dynamic-system-prompt-sections`, `--strict-mcp-config --mcp-config '{"mcpServers":{}}'`.

**Tool-restricted:** Planner (`--tools "Read,Glob"`), divergence-resolver (`--tools "Read,Edit,Bash"`).

**Investigator restriction:** preflight-issue, improve scan get `--disallowedTools "Edit Write NotebookEdit"`.

**Full-tool:** Implementer, Reviewer, Merger — no restriction.

## Divergence-resolver no-CHECKS contract

Resolver does textual conflict resolution only; prompt forbids running `{{CHECKS}}`. Post-merge breakage detected by `get_safe_sha()` preflight pass after fast-forward. Trade-off: main briefly carries a potentially broken merge until preflight-fix repairs it.

## Considered Options

- **Expose flag profiles in `STAGE_OVERRIDES`.** Rejected: tool/permission surface is contract, not config.
- **Keep resolver running CHECKS.** Rejected: cleaner separation — resolver does textual merge, preflight-issue does diagnosis.

## Consequences

- `build_command` gains `role: AgentRole`; per-role flag lookup in same module.
- `coordination/diverge.md` forbids `{{CHECKS}}`; placeholder dropped from scope.
- Token savings largest for Planner and divergence-resolver.
- `--exclude-dynamic-system-prompt-sections` improves prompt-cache hit rate across parallel runs.
