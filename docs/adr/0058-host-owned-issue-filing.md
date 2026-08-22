# Host-owned issue filing

Until ADR 0015, improve-mode agents filed issues by calling `gh issue create --body-file <path>` themselves. Filing from inside the container introduced two problems: (a) a container crash or `UsageLimitError` after a partial batch left some issues created and others not, with no durable record to distinguish them on resume; (b) wiring inter-issue relationships (sub-issue registration, native dependency edges) required the agent to make a series of `gh` calls with no atomicity guarantee, so a mid-wire interruption left the issue graph in a partially-connected state.

Host-owned filing replaces agent-driven `gh` calls with a host-side `file_draft_set` function. Agents write structured draft files to `_drafts/` inside the role session dir; the host reads them and files atomically — from the agent's perspective filing is a side effect of writing files correctly, not an explicit tool-call sequence.

## Two-stage commit

`file_draft_set` applies a two-stage commit to every candidate's issue set:

**Stage 1** creates every issue (spec + slices) without the state label, then wires all sub-issue and dependency edges. Issues exist on GitHub but are invisible to `cfg.issue_label` queries, so the Planner does not pick them up in a racing iteration.

**Stage 2** applies the state label to every *slice* in the set once Stage 1 is complete. Only after Stage 2 are the slices ready-for-agent.

*Amended: Stage 2 originally read "every issue in the set", which put the state label on the improve spec too. The spec is a tracking parent and carries no state label — a labelled spec reaches the slice classifier (ADR 0056), is given a slice-mode label, and becomes implementable work. The bundled PRD prompt and the glossary always said so; this ADR and `file_draft_set` did not.*

The separation guarantees that a partial run never leaves issues in the `ready-for-agent` state with broken dependency graphs: the Planner can only see issues whose full graph is already wired.

## Blocking written twice

Each slice's blockers are recorded in two complementary forms:

- **Body text:** `Blocked by #N, #M` in the issue body so a human reader can see the relationship without a GitHub API call, and so the Planner's judgement gate has the relationship in the prose it reads.
- **Native dependencies:** `add_issue_dependency(child_number, blocker_database_id)` registers a first-class GitHub issue dependency edge so the dependency graph is queryable via the API and visible in the GitHub UI.

Amended by ADR 0059: this section originally claimed the body line served "the Planner's plain-text scan". No such scan existed — nothing read either form. ADR 0059 makes the native edge the mechanical gate and keeps the body line for human readers and for the Planner's judgement gate.

Both forms are written for every intra-candidate blocking edge and for every cross-spec blocker (see below).

## Cross-spec blocking chain

When the scan returns multiple candidates, each candidate's slices are blocked on the previous candidate's spec issue. This serialises candidate work so the second candidate's implementation does not start until the first candidate's spec is closed. The `prev_spec` tuple `(spec_number, spec_database_id)` is read from the previous candidate's durable record and passed to `file_draft_set`, which appends the cross-spec blocker to every slice's body text and registers it as a native dependency edge.

## Fail-fast-with-a-durable-record

`file_draft_set` reads and writes a `_candidate_record` JSON file at `candidates/<N>/_candidate_record` in the role session dir. The record tracks:

- `spec_number` / `spec_database_id` / `spec_title` — set after the spec issue is created (Stage 1a).
- `filed_slices` — list of `{handle, number, database_id, title}` entries, one appended after each slice is created (Stage 1b); handles are the draft's intra-set reference keys.
- `labels_applied` — set to `True` after Stage 2 completes.

Each write is immediate. On resume `file_draft_set` reads the record and skips already-filed issues (`filed_handles` set). This makes every step idempotent: a crash anywhere in the pipeline leaves enough state for the next run to continue from the exact point of interruption rather than re-creating issues or re-applying labels. A corrupt or missing record restarts Stage 1 from scratch — a duplicate spec may be created on GitHub in that edge case, but it carries no state label and is left for manual cleanup.

## Considered Options

- **Agent calls `gh` directly (status quo).** Rejected: no durable record of which issues were filed, so interruption mid-batch leaves an unrecoverable partial state. Cross-issue wiring has the same problem.
- **Promise-marker protocol for each filed issue.** Rejected: extends the orchestrator's coupling to mid-stream agent output; protocol reprompts complicate the retry path; still has no mechanism for wiring dependency edges.
- **Single atomic GitHub API transaction.** Not available: GitHub's REST API has no multi-issue transaction primitive. The two-stage label approach is the practical approximation.
- **Per-candidate draft dir (`candidates/<N>/_drafts/`).** Rejected: the host reads from a fixed role-level `_drafts/` path; threading the active candidate index into the reader would add coupling with no benefit since `_drafts/` is cleared between candidates anyway (see ADR 0015 amendment).

## Consequences

- `_candidate_record` participates in role-session-dir invariants. Any future refactor of the layout, preservation rule, or session cleanup must keep `candidates/<N>/_candidate_record` readable across worktree teardown and resume.
- The state label is never visible mid-filing. Monitoring tools that watch for `ready-for-agent` issues will not see partially-wired candidates.
- A crash between Stage 1 completion and Stage 2 completion leaves issues without the state label. On resume `labels_applied=False` re-enters Stage 2 and applies the label; no duplicate issues are created.
- Cross-spec serialisation means multi-candidate runs produce a dependency chain: candidate 1's slices block on candidate 0's spec, candidate 2's slices block on candidate 1's spec, and so on. This is intentional — it prevents the Planner from picking up later candidates before earlier ones are reviewed and merged.
