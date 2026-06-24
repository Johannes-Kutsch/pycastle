# Shared operator-actionable agent credential failures

Agent-provider credential and account-access failures route by owner, not status code. When any agent service identifies a credential failure the local operator can repair, pycastle files or reuses one family-level issue on the consuming project's tracker with `bug` + `needs-triage`, redacts credential material, bypasses `auto_file_issue`/`bug_report_repo`, and stops the current run. Independent of `auto_file_bugs`.

**Dedup:** single `operator-actionable agent credential failure` family per project. Only open issues reused; closed ones are historical. Title is stable; service-specific evidence in body.

**Issue body:** generic operator action first (repair credentials, rerun pycastle), states this is about local credentials not a source-code defect, includes provider-specific remediation when signature is specific enough.

**Classification:** consumes provider error observations (service, raw text, optional status/code, source stream) before final hard-error bucketing. Credential signatures are allowlist-only; broad auth words like `401`/`unauthorized` stay on hard-error paths unless provider-specific evidence justifies reclassification.

## Scope

First implementation: Codex `refresh_token_reused` and equivalent prose, Codex missing host auth, Claude disabled-subscription-access phrase, OpenCode invalid API key / `401` invalid-key.

**Excluded:** missing credentials for unreferenced services, quota/rate limits with reset times, transient outages, model/config mistakes, GitHub/Git auth, prompt/protocol failures.

## Policies

- Credential failures are terminal (exit non-zero) but not preservation-worthy by themselves.
- No fallback to configured stage alternatives — unusable credentials must not be hidden.
- Applies to every role including diagnostics. Original failure may stay undiagnosed until credentials repaired.
- If tracker access unavailable, print locally and stop.
- Open credential issue is not a project-level lock — pycastle reuses only after observing a fresh failure.

This supersedes the earlier Claude subscription-access-denial-as-permanent-exhaustion behavior and the narrower Codex-only auth-lineage policy. Amends ADR 0023's hard-error path: credential failures are not upstream pycastle bugs.
