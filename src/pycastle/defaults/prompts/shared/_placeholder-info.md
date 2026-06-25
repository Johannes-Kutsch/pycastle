Prompt templates use a two-tier placeholder model: *global* placeholders are available in every template; *scope* placeholders are specific to a group of templates that share the same runtime context. Within the prompt-family tree, underscore-prefixed files are fragments or reference cards (`work/_shared-instructions.md`, `work/_output-rules.md`, `shared/_issue-tracker.md`, `shared/_placeholder-info.md`, `shared/standards/_*.md`); dispatched prompts such as `shared/resume.md` keep non-underscore names.

## Global placeholders

- `{{BUG_LABEL}}` — GitHub label applied to bug reports.
- `{{READY_FOR_AGENT_LABEL}}` — GitHub label marking issues ready for the agent to pick up.
- `{{READY_FOR_HUMAN_LABEL}}` — GitHub label requesting human review.
- `{{ENHANCEMENT_LABEL}}` — GitHub label for enhancement requests.
- `{{NEEDS_TRIAGE_LABEL}}` — GitHub label for issues awaiting triage.
- `{{NEEDS_INFO_LABEL}}` — GitHub label for issues awaiting more information.
- `{{WONTFIX_LABEL}}` — GitHub label for issues that will not be fixed.
- `{{REFACTOR_SLICE_LABEL}}` — GitHub label marking refactor-slice issues.
- `{{BEHAVIOR_SLICE_LABEL}}` — GitHub label marking behavior-slice issues.
- `{{DOCS_SLICE_LABEL}}` — GitHub label marking docs-slice issues.
- `{{FEEDBACK_COMMANDS}}` — Formatted list of implement-feedback commands (e.g. `ruff check --fix` and `pytest`).
- `{{CHECKS}}` — All preflight check commands joined with `&&`.
- `{{DESIGN_STANDARDS}}` — Contents of `shared/standards/_design.md` (architecture vocabulary and deepening strategy).
- `{{IMPLEMENTATION_STANDARDS}}` — Contents of `shared/standards/_implementation.md` (testing, mocking, deep modules, interfaces, and refactoring).
- `{{IMPLEMENT_OUTPUT_RULES}}` — Contents of `work/_output-rules.md` (commit-message tag format and convention, do-not-stage rule).
- `{{EXPECTED_OUTPUT_SHAPE}}` — Prompt-specific output-shape fragment inserted into the `<output>` section for templates that require host output contracts.
- `{{WORK_SHARED_INSTRUCTIONS}}` — Contents of `work/_shared-instructions.md` (shared task/context, interrupted-work, explore, glossary, and ADR framing used by `work/behavior.md`, `work/refactor.md`, `work/docs.md`, and `work/review.md`).
- `{{ISSUE_TRACKER}}` — Contents of `shared/_issue-tracker.md` (GitHub `gh` CLI recipes for issue operations: create, view, search, label, comment, close, sub-issue). Resolved through the same per-file override rule as other shared fragments.

## Scope: PER_ISSUE

Used by: work/behavior.md, work/refactor.md, work/docs.md, work/review.md

- `{{ISSUE_NUMBER}}` — The GitHub issue number.
- `{{ISSUE_TITLE}}` — The issue title.
- `{{ISSUE_BODY}}` — The issue body markdown.
- `{{ISSUE_COMMENTS}}` — Formatted issue comments.
- `{{BRANCH}}` — The working branch name.
- `{{INTERRUPTED_WORK}}` — Interrupted-work clause rendered only for FRESH dispatch on a dirty working tree; tells the agent to inspect `git diff` and `git status`; empty string otherwise.

## Scope: MERGE

Used by: coordination/merge.md

- `{{BRANCHES}}` — Newline-separated list of branches to merge.

## Scope: PLAN

Used by: coordination/plan.md

- `{{ALL_OPEN_ISSUES_JSON}}` — JSON array of all open issues.
- `{{READY_FOR_AGENT_ISSUES_JSON}}` — JSON array of issues labelled ready-for-agent.

## Scope: PREFLIGHT

Used by: diagnostics/preflight-issue.md

- `{{CHECK_NAME}}` — Name of the failing preflight check.
- `{{COMMAND}}` — The preflight check command that failed.
- `{{OUTPUT}}` — Output produced by the failing check.

## Scope: HOST_CHECK

Used by: diagnostics/host-check-issue.md

- `{{HOST_OS}}` — Host operating system where the failing host check was observed.
- `{{HOST_PLATFORM}}` — Host platform identifier where the failing host check was observed.
- `{{CHECKED_SHA}}` — Git SHA checked by `pycastle check`.
- `{{CHECK_NAME}}` — Name of the failing host check.
- `{{COMMAND}}` — The host check command that failed.
- `{{OUTPUT}}` — Output produced by the failing host check.

## Scope: IMPROVE_SCAN

Used by: improve/01-scan.md

- `{{RECENT_IMPROVE_PRD_TITLES}}` — Plain-text recent Improve PRD title history for novelty checking, or `No recent improve PRDs found.` when the lookup is empty.

## Scope: IMPROVE_SESSION

Used by: improve/02-prd.md, improve/04-no-candidate-report.md

- `{{IMPROVE_SHORT_SID}}` — Short session ID for the improve session.
- `{{RECENT_IMPROVE_PRDS}}` — Plain-text recent Improve PRD history for novelty checking, or `No recent improve PRDs found.` when the lookup is empty.

## Scope: IMPROVE_ISSUES

Used by: improve/03-issues.md

- `{{IMPROVE_SHORT_SID}}` — Short session ID for the improve session.
- `{{ISSUE_NUMBER}}` — The GitHub issue number.
- `{{ISSUE_TITLE}}` — The issue title.
- `{{ISSUE_BODY}}` — The issue body markdown.
- `{{ISSUE_COMMENTS}}` — Formatted issue comments.

## Scope: DIVERGE

Used by: coordination/diverge.md

- `{{BRANCH}}` — The current branch name (used to identify both the local branch and its `origin/<branch>` counterpart to merge).

## Scope: RESUME

Used by: shared/resume.md

*(no scope-specific placeholders)*

## Scope: FAILURE_REPORT

Used by: diagnostics/failure-report.md

- `{{FAILED_ROLE}}` — The agent role that failed.
- `{{SESSION_DIR}}` — Path to the failed service's role-session directory.
- `{{EVIDENCE_PATH}}` — Path in the worktree where the copied invocation log is available.
- `{{HAS_EVIDENCE_PATH}}` — "yes" when evidence is available, otherwise "no".
- `{{FAILURE_CLASS}}` — Classification of the failure: `"protocol_error"` (reprompt-loop exhaustion) or `"non_typed_crash"` (untyped exception on resume retry).
