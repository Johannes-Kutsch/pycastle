# Destination-scoped auto-issue gate, default off

`auto_file_bugs` gates *every* auto-filed issue whose destination is the upstream pycastle repo (`bug_report_repo`, default `Johannes-Kutsch/pycastle`) — regardless of trigger severity. Default `False`. A single helper `auto_file_issue(title, body, labels, *, cfg)` owns gate check, token resolution, API attempt via `GithubService.create_issue_in`, and prefilled-URL fallback. Consuming-project agent-filed issues (e.g. `AgentRole.PREFLIGHT_ISSUE` writing to the user's own repo) are unaffected — gate is destination-scoped, not severity-scoped.

Trigger was #808: with the planned usage-limit-parse-failure reporter (#807), non-developer users would file recurring noise into upstream on each format drift. The previous default `True` was safe only with one filing site; a second forces the question of what the flag denotes.

## Considered Options

- **Per-site flags (`auto_file_bugs` for crashes, future flags per site).** Rejected: each new site adds a knob; mental model degrades to a matrix. Real axis is destination, not severity.
- **Severity-scoped gate (crashes always file; soft reports gated).** Rejected: conflates "did the user crash?" with "ping upstream?"; forces every site classified on a severity axis the maintainer doesn't care about.
- **Detect dev-checkout at startup.** Rejected: heuristic wrong in forks, CI, contributors-via-fork.
- **Lint/AST rule against bypass.** Rejected: structure not behaviour; helper-funnel achieves it by construction.
- **Destination-scoped single gate — chosen.** "I want pycastle to file issues into `bug_report_repo` on my behalf." Default `False`. One helper, one mental model, one place to add future sites.

## Consequences

- `Config.auto_file_bugs` default flips from `True` to `False` in `src/pycastle/config/loader.py`. Scaffolded `config.py` documents the developer-opt-in posture.
- `bug_reporter.py` consolidates: `auto_file_issue(title, body, labels, *, cfg) -> None` is the single public entry. Owns gate, token resolution (`_safe_resolve_token`), API attempt (`_try_api_path`), prefilled-URL construction (`build_bug_report_url` with truncation), printed line. `report_and_exit` is a thin caller: print traceback → call helper → `sys.exit(1)`. `build_bug_report_url` becomes internal.
- Body composition stays with caller (each site composes its own `## Traceback` / `## Failed message`); helper prepends shared pycastle/Python/OS env block.
- `UsageLimitError` gains `raw_message: str | None`; `_check_usage_limit` (Claude) / `_extract_usage_limit` / `_parse_reset_time` (Codex) pass unparsed line when they identify a usage-limit they can't fully parse. The `run_iteration` boundary handler (ADR 0008) is the single place that calls `auto_file_issue` for this case and owns per-process dedupe keyed by message signature.
- HITL config-flip is a separate `ready-for-human` issue (one-time per developer machine). No CLI helper.
- Consuming-project agent-filed issues (PREFLIGHT_ISSUE, failure-report, improve/02–04 via `{{ISSUE_TRACKER}}` per ADR 0021) out of scope — they file into cwd-derived repo, not `bug_report_repo`.
- Test in `tests/test_bug_reporter.py` asserts with `auto_file_bugs=False` + reachable token, API path not attempted; prefilled URL is the only output. Parametrized regression covers usage-limit-parse-failure boundary path.
