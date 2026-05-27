# ~~Discipline-forcing artifact tags for behavior-slice implement and review~~

**Superseded.** Host-side enforcement of `<behavior>`, `<reviewed_diff>`, and `<checks_passed>` tags removed (#917). Tags remain in prompts as workflow scaffolding — they guide the agent through the review/implement steps — but the orchestrator no longer parses or requires them. `ReviewerOutput` collapsed into `CommitMessageOutput`; `BehaviorParseError`, `ReviewedDiffParseError`, `ChecksPassedParseError` retired.

**Why:** The enforcement cost exceeded its value. Reviewer agents that completed their work but emitted `<commit_message>` without `<reviewed_diff>` were rejected with a generic reprompt that didn't name the missing tag, causing three futile retries and a `protocol_error`. The `<behavior>` enforcement was never wired into the production code path (`container_runner` never passed `behavior_slice=True`). Occasional agent drift on TDD discipline is acceptable; hard failures on tag omission are not.
