"""Deduped upstream issue filing — shared mechanic for all filing sites."""

from __future__ import annotations

from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from pycastle.services import GithubService


def file_deduped_upstream_issue(
    dedupe_query: str,
    title: str,
    body: str,
    labels: list[str],
    github_svc: GithubService,
    *,
    echo: bool = True,
) -> int | None:
    """Search for an existing open issue matching *dedupe_query*; return its
    number if found. Otherwise create a new issue and return its number.

    Returns ``None`` when ``create_issue_in`` raises ``GithubServiceError``.
    ``GithubServiceError`` from the search is swallowed and treated as "no
    existing match". Other exceptions from either call propagate unchanged.

    Pass ``echo=False`` when the caller owns its own "Filed issue" output line.
    """
    from pycastle.services import GithubServiceError

    try:
        existing = github_svc.search_open_issues_by_title(dedupe_query)
    except GithubServiceError:
        existing = []

    if existing:
        return existing[0]

    try:
        number, _ = github_svc.create_issue_in(github_svc.repo, title, body, labels)
        if echo:
            click.echo(f"Filed issue #{number} on {github_svc.repo}: {title}")
    except GithubServiceError:
        return None
    else:
        return number
