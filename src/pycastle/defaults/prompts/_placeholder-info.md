Prompt templates use a two-tier placeholder model: *global* placeholders are available in every template; *scope* placeholders are specific to a group of templates that share the same runtime context.

## Global placeholders

- `{{BUG_LABEL}}` — GitHub label applied to bug reports.
- `{{READY_FOR_AGENT_LABEL}}` — GitHub label marking issues ready for the agent to pick up.
- `{{READY_FOR_HUMAN_LABEL}}` — GitHub label requesting human review.
- `{{ENHANCEMENT_LABEL}}` — GitHub label for enhancement requests.
- `{{NEEDS_TRIAGE_LABEL}}` — GitHub label for issues awaiting triage.
- `{{NEEDS_INFO_LABEL}}` — GitHub label for issues awaiting more information.
- `{{WONTFIX_LABEL}}` — GitHub label for issues that will not be fixed.
- `{{FEEDBACK_COMMANDS}}` — Formatted list of implement-feedback commands (e.g. `ruff check --fix` and `pytest`).
- `{{CHECKS}}` — All preflight check commands joined with `&&`.
- `{{TESTING_STANDARDS}}` — Contents of `coding-standards/tests.md`.
- `{{MOCKING_STANDARDS}}` — Contents of `coding-standards/mocking.md`.
- `{{INTERFACES_STANDARDS}}` — Contents of `coding-standards/interfaces.md`.
- `{{DEEP_MODULES_STANDARDS}}` — Contents of `coding-standards/deep-modules.md`.
- `{{REFACTORING_STANDARDS}}` — Contents of `coding-standards/refactoring.md`.
- `{{LANGUAGE_STANDARDS}}` — Contents of `coding-standards/language.md`.
- `{{DEEPENING_STANDARDS}}` — Contents of `coding-standards/deepening.md`.

## Scope: PER_ISSUE

Used by: implement-prompt.md, review-prompt.md

- `{{ISSUE_NUMBER}}` — The GitHub issue number.
- `{{ISSUE_TITLE}}` — The issue title.
- `{{ISSUE_BODY}}` — The issue body markdown.
- `{{ISSUE_COMMENTS}}` — Formatted issue comments.
- `{{BRANCH}}` — The working branch name.

## Scope: MERGE

Used by: merge-prompt.md

- `{{BRANCHES}}` — Newline-separated list of branches to merge.

## Scope: PLAN

Used by: plan-prompt.md

- `{{ALL_OPEN_ISSUES_JSON}}` — JSON array of all open issues.
- `{{READY_FOR_AGENT_ISSUES_JSON}}` — JSON array of issues labelled ready-for-agent.

## Scope: PREFLIGHT

Used by: preflight-issue.md

- `{{CHECK_NAME}}` — Name of the failing preflight check.
- `{{COMMAND}}` — The preflight check command that failed.
- `{{OUTPUT}}` — Output produced by the failing check.

## Scope: IMPROVE_SCAN

Used by: improve/01-scan.md

*(no scope-specific placeholders)*

## Scope: IMPROVE_SESSION

Used by: improve/02-prd.md, improve/04-no-candidate-report.md

- `{{IMPROVE_SHORT_SID}}` — Short session ID for the improve session.

## Scope: IMPROVE_ISSUES

Used by: improve/03-issues.md

- `{{IMPROVE_SHORT_SID}}` — Short session ID for the improve session.
- `{{ISSUE_NUMBER}}` — The GitHub issue number.
- `{{ISSUE_TITLE}}` — The issue title.
- `{{ISSUE_BODY}}` — The issue body markdown.
- `{{ISSUE_COMMENTS}}` — Formatted issue comments.

## Scope: RESUME

Used by: _resume-prompt.md

*(no scope-specific placeholders)*

## Scope: FAILURE_REPORT

Used by: failure-report.md

- `{{FAILED_ROLE}}` — The agent role that failed.
- `{{SESSION_DIR}}` — Path to the session log directory.
