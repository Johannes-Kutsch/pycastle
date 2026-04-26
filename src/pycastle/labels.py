import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

import click

LABELS = [
    {"name": "bug", "description": "Something isn't working", "color": "d73a4a"},
    {"name": "enhancement", "description": "New feature or request", "color": "0d9488"},
    {
        "name": "need-info",
        "description": "Waiting on reporter for more information",
        "color": "b03176",
    },
    {
        "name": "needs-triage",
        "description": "Maintainer needs to evaluate this issue",
        "color": "d6b80d",
    },
    {
        "name": "ready-for-agent",
        "description": "Fully specified, ready for afk agent",
        "color": "0be348",
    },
    {
        "name": "ready-for-human",
        "description": "Requires human implementation",
        "color": "5181b8",
    },
    {"name": "wontfix", "description": "This will not be worked on", "color": "e1e4e8"},
]

_API = "https://api.github.com"


def _get_remote_repo() -> tuple[str, str] | None:
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if "github.com" not in url:
            return None
        path = (
            url.split("github.com/")[-1] if "github.com/" in url else url.split(":")[-1]
        )
        path = re.sub(r"\.git$", "", path)
        owner, repo = path.split("/", 1)
        return owner, repo
    except Exception:
        return None


def _gh(
    method: str, path: str, token: str, data: dict | None = None
) -> tuple[int, object]:
    req = urllib.request.Request(f"{_API}{path}", method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode()
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None


def _resolve_repo(token: str) -> tuple[str, str] | None:
    remote = _get_remote_repo()
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


def create_labels_interactive(token: str) -> None:
    resolved = _resolve_repo(token)
    if not resolved:
        return
    owner, repo = resolved

    reset = click.confirm("Delete all existing labels first?", default=False)

    if reset:
        status, existing = _gh(
            "GET", f"/repos/{owner}/{repo}/labels?per_page=100", token
        )
        if status == 200 and isinstance(existing, list):
            for label in existing:
                _gh(
                    "DELETE",
                    f"/repos/{owner}/{repo}/labels/{urllib.parse.quote(label['name'])}",
                    token,
                )

    counts = {"created": 0, "skipped": 0, "failed": 0}
    failures: list[str] = []
    for label in LABELS:
        status, _ = _gh("POST", f"/repos/{owner}/{repo}/labels", token, label)
        if status == 201:
            counts["created"] += 1
        elif status == 422:
            counts["skipped"] += 1
        else:
            counts["failed"] += 1
            failures.append(f"{label['name']}: HTTP {status}")

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


def main() -> None:
    from dotenv import load_dotenv
    from .config import ENV_FILE

    load_dotenv(ENV_FILE)

    token = os.getenv("GH_TOKEN", "").strip()
    if not token:
        token = click.prompt("GitHub token", hide_input=True)
    if not token:
        click.echo(click.style("Error: no token provided.", fg="red"), err=True)
        sys.exit(1)

    create_labels_interactive(token)
