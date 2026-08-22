# Draft-set validity is filing and workability, not shape

Status: accepted. Lands with #2173.

A **draft set** is rejected only when it cannot be filed or when a slice could not be worked afterwards: no drafts, a missing title, a wrong type, a `blocked_by` handle from outside the set, no spec draft, or a slice that fails **issue readiness**. Labels are required on no draft — the **label pass** applies the state label itself and **issue readiness** owns the slice-mode label, so demanding the field guarded nothing on the spec and duplicated readiness on a slice.

A missing spec draft is now its own rejection rather than a silent promotion of whichever slice sorted first into the parent role.

## Considered Options

Requiring `labels` only on slice drafts was the smaller change and was rejected: on a slice the field is still redundant with readiness, and it asks the agent for the state label that Stage 1 strips and Stage 2 re-applies (ADR 0058) — ceremony that can only drift.

Dropping readiness along with the label requirement was rejected in the other direction: readiness is the only check that encodes "this slice could not be worked", which is half of what validity means here.

## Consequences

The `labels` requirement cost a full improve run. The PRD prompt correctly instructs the agent to write the spec with `title` alone; the validator required `labels` on every draft; and for months no run took the draft path because the PRD agent filed the spec itself. When a prompt revision made the agent finally obey the instruction, the validator rejected work that was entirely fileable, and the run died.

That failure was a contradiction between two internally coherent documents that nobody read together. The guard against a repeat is a test that renders the shipped improve prompts, extracts the frontmatter examples they instruct the agent to follow, and pushes them through the validator, requiring a clean pass — so the prompt and the check cannot drift apart unnoticed again. It lands with this decision.
