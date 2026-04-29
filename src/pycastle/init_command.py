import re
import sys
from importlib.resources import files
from pathlib import Path

import click

_FILES = [
    ".gitignore",
    "config.py",
    "Dockerfile",
    "prompts/plan-prompt.md",
    "prompts/implement-prompt.md",
    "prompts/review-prompt.md",
    "prompts/merge-prompt.md",
    "prompts/preflight-issue.md",
    "prompts/standards/tests.md",
    "prompts/standards/mocking.md",
    "prompts/standards/interfaces.md",
    "prompts/standards/deep-modules.md",
    "prompts/standards/refactoring.md",
]

_ENV_TEMPLATE = "ANTHROPIC_API_KEY=\nCLAUDE_CODE_OAUTH_TOKEN=\nGH_TOKEN=\n"


def _write_env_key(env_file: Path, key: str, value: str) -> None:
    content = env_file.read_text()
    content = re.sub(rf"^{key}=.*$", f"{key}={value}", content, flags=re.MULTILINE)
    env_file.write_text(content)


def _write_config_value(config_file: Path, key: str, value: str) -> None:
    content = config_file.read_text()
    content = re.sub(
        rf'^{key}\s*=\s*"[^"]*"', f'{key} = "{value}"', content, flags=re.MULTILINE
    )
    config_file.write_text(content)


def main() -> None:
    dest = Path("pycastle")
    pkg = files("pycastle").joinpath("defaults")

    for rel in _FILES:
        target = dest / rel
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        src = pkg
        for part in rel.split("/"):
            src = src / part
        try:
            target.write_bytes(src.read_bytes())
        except Exception as e:
            click.echo(
                click.style(f"Error: could not write {target} — {e}", fg="red"),
                err=True,
            )
            sys.exit(1)

    config_file = dest / "config.py"
    image_name = re.sub(r"[^a-z0-9]+", "-", Path.cwd().name.lower()).strip("-")
    try:
        _write_config_value(config_file, "docker_image_name", image_name)
    except Exception as e:
        click.echo(
            click.style(
                f"Error: could not set docker_image_name in {config_file} — {e}",
                fg="red",
            ),
            err=True,
        )
        sys.exit(1)

    env_file = dest / ".env"
    if not env_file.exists():
        try:
            env_file.write_text(_ENV_TEMPLATE)
        except Exception as e:
            click.echo(
                click.style(f"Error: could not write {env_file} — {e}", fg="red"),
                err=True,
            )
            sys.exit(1)

    gh_token = click.prompt(
        "GitHub token (press Enter to skip)",
        default="",
        hide_input=True,
        show_default=False,
    )
    if gh_token:
        try:
            _write_env_key(env_file, "GH_TOKEN", gh_token)
        except Exception as e:
            click.echo(
                click.style(f"Error: could not save GH_TOKEN — {e}", fg="red"), err=True
            )
            sys.exit(1)

    claude_token = click.prompt(
        "Claude token (press Enter to skip)",
        default="",
        hide_input=True,
        show_default=False,
    )
    if claude_token:
        try:
            _write_env_key(env_file, "CLAUDE_CODE_OAUTH_TOKEN", claude_token)
        except Exception as e:
            click.echo(
                click.style(
                    f"Error: could not save CLAUDE_CODE_OAUTH_TOKEN — {e}", fg="red"
                ),
                err=True,
            )
            sys.exit(1)

    if not claude_token:
        click.echo(
            "Set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN in pycastle/.env before running pycastle."
        )

    click.echo()
    if gh_token and click.confirm("Create GitHub labels?", default=False):
        from .labels import create_labels_interactive

        create_labels_interactive(gh_token)

    click.echo()
