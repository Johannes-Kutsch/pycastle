# ADR 0064 — Improve-spec title prefix cuts over in one step

## Context

The improve phase files a tracking issue (the **improve spec**) whose title carries a magic prefix. `get_recent_improve_specs` searches GitHub for open issues matching that prefix and returns the titles to the scan agent as its novelty history (`RECENT_IMPROVE_SPEC_TITLES` in the `IMPROVE_SCAN` prompt scope). The novelty gate uses this list to avoid re-nominating ideas that prior improve runs already addressed.

The prefix is being renamed from `[improve-PRD]` to `[improve-spec]` to align with the project's ubiquitous language, where **improve spec** is the canonical term and PRD is deprecated.

## Decision

The cutover happens in one step: the filing code writes `[improve-spec]` and `get_recent_improve_specs` searches only for `[improve-spec]`. No dual-read of both `[improve-PRD]` and `[improve-spec]` is performed.

### Rejected alternative: dual-read

A dual-read implementation would have searched for both prefixes and merged the result lists, preserving pre-cutover specs in the novelty history. This was rejected because it would leave two active prefixes in the system indefinitely. Removing the dual-read later would require a second cutover decision and a coordinating ticket, and the benefit — a handful of pre-cutover specs staying visible to the novelty gate — does not justify that ongoing complexity.

### Accepted cost

After the cutover, `get_recent_improve_specs` returns only specs filed under the new prefix. Any improve spec filed before the cutover is invisible to the novelty gate. As a result, the scan agent may re-nominate an idea that a pre-cutover improve run already addressed, filing a duplicate or near-duplicate spec.

This is acceptable: the novelty gate is a soft quality signal, not a hard constraint. A duplicate nomination costs one improve slot and produces a spec that a human reviewer or the AFK-safety filter can reject or close. The alternative — permanently maintaining two search paths — costs ongoing code complexity for every future reader and maintainer.
