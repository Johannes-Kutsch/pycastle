# Shared operator-actionable agent credential failures

Agent-provider credential and account-access failures are routed by owner, not by provider status code. When Claude, Codex, OpenCode, or a future agent service identifies a provider-specific credential failure that the local operator can repair, pycastle files or reuses one family-level issue on the consuming project's tracker with `bug` + `needs-triage`, preserves the raw provider diagnostic wording plus minimal role/service context while redacting credential material, makes no tracker mutation when that open family issue already exists, bypasses `auto_file_issue` / `bug_report_repo`, and stops the current run. This consuming-project filing path is independent of `auto_file_bugs`, which gates upstream pycastle bug reports only. The issue does not receive `ready-for-agent` because an AFK agent cannot repair local provider credentials or account access.

The consuming-project issue deduplicates by the single `operator-actionable agent credential failure` family for that project, not by service, role, stage, worktree, model, or exact provider phrase. Only open issues are reused; closed credential-failure issues are historical records, so a later recurrence creates a new issue. The issue title is family-specific and stable; service-specific evidence belongs in the body.

The issue body gives the generic operator action first: repair local agent credentials/account access and rerun pycastle. It states narrowly that the issue is about local agent-provider credentials/account access, not a source-code defect in the consuming repository. When the provider signature is specific enough, the body also includes provider-specific remediation such as running `codex login`, restoring Claude Code subscription access or switching to a token with access, or updating the configured OpenCode API key.

An open credential-failure issue is not a project-level runtime lock. Pycastle reuses it only after observing a fresh credential failure; it does not abort merely because the tracker already contains an open credential issue.

When an open credential issue is reused, pycastle prints a concise local status line with the existing issue number or URL. That local status is not a tracker mutation.

If consuming-project issue filing or lookup fails because tracker access is unavailable, pycastle prints the credential failure and remediation locally and stops rather than falling back to upstream bug filing.

Classification consumes provider error observations before final hard-error bucketing: the service, raw provider text, optional status/code/error name, and source stream such as JSON event, stderr, or pre-dispatch host check. This is required because a provider may expose the credential signature in stderr while the terminal event contains only equivalent prose. Credential signatures are allowlist-only: broad status or auth words such as `401`, `403`, `unauthorized`, `invalid_grant`, or `missing bearer` remain on existing hard-error paths unless provider-specific evidence proves local operator repair is the right owner.

Provider adapters own the provider-specific signatures. The shared policy owns the observation shape, shared credential-failure result, redacted evidence handling, consuming-project issue filing/reuse, local status, and terminal run outcome.

Credential failures surface to the iteration boundary as a distinct result or exception, not as generic `HardAgentError` metadata. This keeps consuming-project issue routing, no-fallback behavior, no preservation-by-default, `auto_file_bugs` independence, family dedupe, and remediation messaging out of the generic hard API error path.

New provider credential condition families require a docs update explaining why the owner is local operator action. Additional wording variants for an already documented condition can be handled as implementation/test detail when they do not change the condition boundary.

The first implementation slice is limited to the observed or high-confidence signatures already discussed: Codex `refresh_token_reused`, Codex equivalent refresh-token-already-used prose, Codex missing host auth before dispatch, Claude's disabled Claude Code subscription-access phrase, and OpenCode structured invalid API key / `401` invalid-key messages. Other provider auth-looking failures remain on existing hard-error, usage-limit, or transient paths until concrete provider evidence justifies documenting a new condition family.

When one invocation yields multiple provider observations, the first high-confidence credential signature determines the route. The filed issue should still include the best redacted diagnostic bundle available from that invocation, such as both the matching stderr snippet and the terminal JSON error when both explain the same failure.

Missing credentials are in scope only for selected or otherwise referenced services at runtime. Credentials for unreferenced services remain irrelevant and do not create operator-actionable issues.

Credential failures are terminal for the current run and exit non-zero, but are not preservation-worthy by themselves. Worktrees and session state are preserved only when existing dirty/resumable-work rules independently require preservation; the credential failure issue carries the provider evidence needed for operator repair.

Credential failures do not fall through to configured stage fallbacks. A fallback provider must not hide unusable local credentials for a selected stage candidate.

The policy applies to every agent role, including diagnostic roles such as the Failure-Report agent. If credentials fail while diagnosing another failure, pycastle files or reuses the same credential issue and names the diagnostic role in the context; the original failure may remain undiagnosed until credentials are repaired.

This supersedes ADR 0032's Claude subscription-access-denial behavior and ADR 0042's narrower Codex-only auth-lineage policy. Claude subscription denial is no longer treated as permanent Claude account exhaustion that can fall through to standby or stage fallback, and Codex auth-lineage exhaustion now follows the shared credential-failure family. It also amends ADR 0023's hard-error path: credential failures are not upstream pycastle hard API bugs, while non-credential hard provider errors remain on the existing hard-error path. Provider adapters own narrow signature detection, such as Claude's disabled-subscription phrase, Codex's `refresh_token_reused` or equivalent refresh-token-already-used terminal prose, and OpenCode invalid-key messages; quota/rate limits with reset times, transient provider outages, model/config mistakes, GitHub repo authentication, Git remote authentication, and prompt/protocol failures remain outside this policy.
