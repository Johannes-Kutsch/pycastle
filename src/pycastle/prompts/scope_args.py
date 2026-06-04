from __future__ import annotations

from collections.abc import Sequence

from ..session import RunKind

_ISSUE_PLACEHOLDER_KEYS = frozenset(
    {"ISSUE_NUMBER", "ISSUE_TITLE", "ISSUE_BODY", "ISSUE_COMMENTS"}
)


def _format_issue_comments(comments: Sequence[dict[str, str]]) -> str:
    parts: list[str] = []
    for c in comments:
        author = c.get("author") or "unknown"
        when = c.get("created_at") or "unknown time"
        body = c.get("body") or ""
        parts.append(f"## Comment by @{author} at {when}\n\n{body}")
    return "\n\n".join(parts)


def build_issue_scope_args(
    issue: dict, *, extra_scope_args: dict[str, str]
) -> dict[str, str]:
    from .pipeline import PromptRenderError

    collisions = _ISSUE_PLACEHOLDER_KEYS & extra_scope_args.keys()
    if collisions:
        raise PromptRenderError(
            f"extra_scope_args collides with reserved ISSUE_* keys: {collisions}"
        )
    return {
        "ISSUE_NUMBER": str(issue["number"]),
        "ISSUE_TITLE": issue["title"],
        "ISSUE_BODY": str(issue["body"] or ""),
        "ISSUE_COMMENTS": _format_issue_comments(issue["comments"]),
        **extra_scope_args,
    }


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
