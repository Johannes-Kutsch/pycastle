import sys

import click

from ..config import Config, load_config, load_credential_env
from ..label_catalog import CANONICAL_LABEL_SPECS
from ..services import (
    GithubAPIError,
    GithubAuthError,
    GithubService,
    GitService,
)


def _resolve_repo(
    git_service: GitService | None = None, cfg: Config | None = None
) -> tuple[str, str] | None:
    svc = git_service or GitService(cfg or load_config())
    remote = svc.get_github_remote_repo()
    if remote:
        owner, repo = remote
        if click.confirm(f"Target repo {owner}/{repo}?", default=True):
            return owner, repo
    slug = click.prompt("Enter repo slug (e.g. torvalds/linux)")
    if "/" not in slug:
        click.echo(
            click.style("Error: invalid format, expected owner/repo.", fg="red"),
            err=True,
        )
        return None
    owner, repo = slug.split("/", 1)
    return owner, repo


def create_labels_interactive(
    token: str,
    git_service: GitService | None = None,
    cfg: Config | None = None,
    github_service: GithubService | None = None,
) -> None:
    _cfg = cfg or load_config()
    labels = [
        {
            "name": getattr(_cfg, spec.config_field),
            "description": spec.description,
            "color": spec.color,
        }
        for spec in CANONICAL_LABEL_SPECS
    ]

    resolved = _resolve_repo(git_service, _cfg)
    if not resolved:
        return
    owner, repo = resolved

    service = github_service or GithubService(f"{owner}/{repo}", token, _cfg)
    try:
        service.check_auth()
    except GithubAuthError as exc:
        click.echo(click.style(f"Error: {exc.body}", fg="red"), err=True)
        sys.exit(1)

    reset = click.confirm("Delete all existing labels first?", default=False)

    if reset:
        try:
            existing = service.list_labels()
        except GithubAPIError:
            existing = []
        for label in existing:
            try:
                service.delete_label(label["name"])
            except GithubAPIError:
                pass

    counts = {"created": 0, "skipped": 0, "failed": 0}
    failures: list[str] = []
    for label in labels:
        try:
            service.create_label(label)
            counts["created"] += 1
        except GithubAPIError as exc:
            if exc.status == 422:
                counts["skipped"] += 1
            else:
                counts["failed"] += 1
                failures.append(f"{label['name']}: HTTP {exc.status}")

    for name in failures:
        click.echo(
            click.style(f"Error: failed to create label {name}.", fg="red"), err=True
        )

    parts = []
    if counts["created"]:
        parts.append(f"Created {counts['created']} labels.")
    if counts["skipped"]:
        parts.append(f"{counts['skipped']} skipped.")
    if counts["failed"]:
        parts.append(f"{counts['failed']} failed.")
    if parts:
        click.echo(" ".join(parts))

    if counts["failed"] or counts["skipped"]:
        click.echo("To rerun: pycastle labels")


def main(cfg: Config | None = None) -> None:
    cfg = cfg or load_config()
    resolved = load_credential_env()

    token = resolved.get("GH_TOKEN", "").strip()
    if not token:
        token = click.prompt("GitHub token", hide_input=True)
    if not token:
        click.echo(click.style("Error: no token provided.", fg="red"), err=True)
        sys.exit(1)

    create_labels_interactive(token, cfg=cfg)
