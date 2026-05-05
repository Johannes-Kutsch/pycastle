from __future__ import annotations

import os
import re
import sys
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Literal

import click

from .config.loader import derive_docker_image_name, resolve_global_dir

_PROJECT_SHAPED_FILES = [
    ".gitignore",
    "Dockerfile",
    "prompts/plan-prompt.md",
    "prompts/implement-prompt.md",
    "prompts/review-prompt.md",
    "prompts/merge-prompt.md",
    "prompts/preflight-issue.md",
    "prompts/coding-standards/tests.md",
    "prompts/coding-standards/mocking.md",
    "prompts/coding-standards/interfaces.md",
    "prompts/coding-standards/deep-modules.md",
    "prompts/coding-standards/refactoring.md",
]

_ENV_TEMPLATE = "ANTHROPIC_API_KEY=\nCLAUDE_CODE_OAUTH_TOKEN=\nGH_TOKEN=\n"


def _write_env_key(env_file: Path, key: str, value: str) -> None:
    content = env_file.read_text()
    content = re.sub(rf"^{key}=.*$", f"{key}={value}", content, flags=re.MULTILINE)
    env_file.write_text(content)


def _fill_commented_hint(config_file: Path, key: str, value: str) -> None:
    content = config_file.read_text()
    content = re.sub(
        rf'^#\s*{key}\s*=\s*"[^"]*"',
        f'# {key} = "{value}"',
        content,
        flags=re.MULTILINE,
    )
    config_file.write_text(content)


def _read_env_values(env_file: Path) -> dict[str, str]:
    if not env_file.exists():
        return {}
    out: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if value:
            out[key] = value
    return out


def _copy_template(rel: str, target: Path, pkg: Traversable) -> None:
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


def _prompt_and_save_credential(env_file: Path, key: str, prompt_text: str) -> str:
    value = click.prompt(prompt_text, default="", hide_input=True, show_default=False)
    if not value:
        return ""
    try:
        _write_env_key(env_file, key, value)
    except Exception as e:
        click.echo(
            click.style(f"Error: could not save {key} — {e}", fg="red"),
            err=True,
        )
        sys.exit(1)
    return value


def refresh() -> None:
    project_dir = Path("pycastle")
    if not project_dir.is_dir():
        click.echo(
            click.style(
                f"Error: no `pycastle/` directory found in {Path.cwd()}; "
                "run `pycastle init` first.",
                fg="red",
            ),
            err=True,
        )
        sys.exit(1)
    pkg = files("pycastle").joinpath("defaults")
    for rel in _PROJECT_SHAPED_FILES:
        _copy_template(rel, project_dir / rel, pkg)


def main(scope: Literal["global", "local"] | None = None) -> None:
    project_dir = Path("pycastle")
    pkg = files("pycastle").joinpath("defaults")

    if scope is None:
        use_global = click.confirm(
            "Scaffold config.py and .env to global pycastle home? (No = local)",
            default=False,
        )
        scope = "global" if use_global else "local"

    pycastle_home = resolve_global_dir(None, os.environ)
    scoped_dir = pycastle_home if scope == "global" else project_dir

    for rel in _PROJECT_SHAPED_FILES:
        target = project_dir / rel
        if target.exists():
            continue
        _copy_template(rel, target, pkg)

    config_file = scoped_dir / "config.py"
    if config_file.exists():
        if scope == "global":
            click.echo(
                f"global config.py already exists at {config_file}; leaving it untouched"
            )
    else:
        _copy_template("config.py", config_file, pkg)

    if scope == "local":
        image_name = derive_docker_image_name(Path.cwd().name)
        try:
            _fill_commented_hint(config_file, "docker_image_name", image_name)
        except Exception as e:
            click.echo(
                click.style(
                    f"Error: could not set docker_image_name in {config_file} — {e}",
                    fg="red",
                ),
                err=True,
            )
            sys.exit(1)

    env_file = scoped_dir / ".env"
    if env_file.exists():
        if scope == "global":
            click.echo(
                f"global .env already exists at {env_file}; leaving it untouched"
            )
    else:
        env_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            env_file.write_text(_ENV_TEMPLATE)
        except Exception as e:
            click.echo(
                click.style(f"Error: could not write {env_file} — {e}", fg="red"),
                err=True,
            )
            sys.exit(1)

    global_env_values = (
        _read_env_values(pycastle_home / ".env") if scope == "global" else {}
    )

    gh_token = global_env_values.get("GH_TOKEN", "")
    if not gh_token:
        gh_token = _prompt_and_save_credential(
            env_file, "GH_TOKEN", "GitHub token (press Enter to skip)"
        )

    claude_token = global_env_values.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if not claude_token:
        claude_token = _prompt_and_save_credential(
            env_file, "CLAUDE_CODE_OAUTH_TOKEN", "Claude token (press Enter to skip)"
        )

    if not claude_token:
        click.echo(
            f"Set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN in {env_file} before running pycastle."
        )

    click.echo()
    if gh_token and click.confirm("Create GitHub labels?", default=False):
        from .labels import create_labels_interactive

        create_labels_interactive(gh_token)

    click.echo()
