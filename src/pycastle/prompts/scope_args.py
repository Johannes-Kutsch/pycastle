from __future__ import annotations

import json
import platform
from collections.abc import Sequence
from typing import Protocol

from .pipeline import PromptRenderError, PromptTemplate, Scope
from ..session import RunKind

_ISSUE_VALUE_KEYS = Scope.PER_ISSUE.placeholders & Scope.IMPROVE_ISSUES.placeholders


class FailureReportSource(Protocol):
    role_value: str
    failure_class: str

    @property
    def session_dir(self) -> str: ...


def _format_issue_comments(comments: Sequence[dict[str, str]]) -> str:
    parts: list[str] = []
    for c in comments:
        author = c.get("author") or "unknown"
        when = c.get("created_at") or "unknown time"
        body = c.get("body") or ""
        parts.append(f"## Comment by @{author} at {when}\n\n{body}")
    return "\n\n".join(parts)


def _validated_scope_args(
    *, subject: str, expected: frozenset[str], scope_args: dict[str, str]
) -> dict[str, str]:
    actual = set(scope_args)
    if actual != expected:
        missing = expected - actual
        extra = actual - expected
        parts: list[str] = []
        if missing:
            parts.append(f"missing: {missing}")
        if extra:
            parts.append(f"extra: {extra}")
        raise PromptRenderError(
            f"scope_args mismatch for {subject}: {'; '.join(parts)}"
        )
    return scope_args


def validated_scope_args_for_scope(
    scope: Scope, scope_args: dict[str, str]
) -> dict[str, str]:
    return _validated_scope_args(
        subject=f"scope {scope.name}",
        expected=scope.placeholders,
        scope_args=scope_args,
    )


def validated_scope_args_for_template(
    template: PromptTemplate, scope_args: dict[str, str]
) -> dict[str, str]:
    return _validated_scope_args(
        subject=f"template {template.name}",
        expected=template.scope.placeholders,
        scope_args=scope_args,
    )


def build_issue_scope_args(
    issue: dict, *, extra_scope_args: dict[str, str]
) -> dict[str, str]:
    collisions = _ISSUE_VALUE_KEYS & extra_scope_args.keys()
    if collisions:
        raise PromptRenderError(
            f"extra_scope_args collides with reserved ISSUE_* keys: {collisions}"
        )
    return {
        "ISSUE_NUMBER": str(issue["number"]),
        "ISSUE_TITLE": issue["title"],
        "ISSUE_BODY": str(issue.get("body") or ""),
        "ISSUE_COMMENTS": _format_issue_comments(issue.get("comments") or []),
        **extra_scope_args,
    }


def build_per_issue_scope_args(
    issue: dict,
    *,
    branch: str,
    run_kind: RunKind,
    is_dirty: bool,
) -> dict[str, str]:
    return build_issue_scope_args(
        issue,
        extra_scope_args={
            "BRANCH": branch,
            "INTERRUPTED_WORK": build_interrupted_work_clause(
                run_kind,
                is_dirty=is_dirty,
            ),
        },
    )


def build_plan_scope_args(
    *, all_open_issues: list[dict], ready_for_agent_issues: list[dict]
) -> dict[str, str]:
    return {
        "ALL_OPEN_ISSUES_JSON": json.dumps(all_open_issues),
        "READY_FOR_AGENT_ISSUES_JSON": json.dumps(ready_for_agent_issues),
    }


def build_merge_scope_args(
    *, conflict_issues: list[dict], active_issue: dict
) -> dict[str, str]:
    from ..iteration.implement import branch_for

    active_branch = branch_for(active_issue["number"])
    return validated_scope_args_for_template(
        PromptTemplate.MERGE,
        {"BRANCHES": f"- {active_branch}"},
    )


def build_preflight_scope_args(
    *, check_name: str, command: str, output: str
) -> dict[str, str]:
    return validated_scope_args_for_template(
        PromptTemplate.PREFLIGHT_ISSUE,
        {
            "CHECK_NAME": check_name,
            "COMMAND": command,
            "OUTPUT": output,
        },
    )


def build_divergence_scope_args(*, branch: str) -> dict[str, str]:
    return validated_scope_args_for_template(
        PromptTemplate.DIVERGENCE_RESOLVE,
        {"BRANCH": branch},
    )


def build_host_check_scope_args(
    *,
    checked_sha: str,
    check_name: str,
    command: str,
    output: str,
    host_os: str | None = None,
    host_platform: str | None = None,
) -> dict[str, str]:
    return validated_scope_args_for_template(
        PromptTemplate.HOST_CHECK_ISSUE,
        {
            "HOST_OS": platform.system() if host_os is None else host_os,
            "HOST_PLATFORM": (
                platform.platform() if host_platform is None else host_platform
            ),
            "CHECKED_SHA": checked_sha,
            "CHECK_NAME": check_name,
            "COMMAND": command,
            "OUTPUT": output,
        },
    )


def build_failure_report_scope_args(failure: FailureReportSource) -> dict[str, str]:
    return validated_scope_args_for_template(
        PromptTemplate.FAILURE_REPORT,
        {
            "FAILED_ROLE": failure.role_value,
            "SESSION_DIR": failure.session_dir,
            "FAILURE_CLASS": failure.failure_class,
        },
    )


def build_interrupted_work_clause(run_kind: RunKind, is_dirty: bool) -> str:
    """Return interrupted-work instructions for fresh dispatches on dirty worktrees."""
    if run_kind != RunKind.FRESH or not is_dirty:
        return ""
    return (
        "\n# Interrupted Work\n\n"
        "This worktree has uncommitted changes from a previous agent run. "
        "Run `git diff` and `git status` to understand the current state, "
        "then continue from where the previous agent left off.\n"
    )
