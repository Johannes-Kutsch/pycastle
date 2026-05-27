from __future__ import annotations

import os
import re
import stat
import sys
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Literal

import click

from ..agents.output_protocol import AgentRole
from ..config.loader import (
    derive_docker_image_name,
    referenced_services,
    resolve_global_dir,
)
from ..session.resume import SESSION_DIR_NAME

_SPECIAL_FILES = {"config.py", ".env", "Dockerfile.claude", "Dockerfile.claude-codex"}


def _discover_project_shaped_files(pkg: Traversable) -> list[str]:
    """Walk the bundled defaults/ tree and return every file path relative to it,
    minus the files init handles separately (scope-aware config.py/.env and the
    service-selected Dockerfile templates).
    """

    def _walk(node: Traversable, prefix: str) -> list[str]:
        out: list[str] = []
        for child in node.iterdir():
            rel = f"{prefix}{child.name}"
            if child.is_dir():
                out.extend(_walk(child, f"{rel}/"))
            else:
                out.append(rel)
        return out

    return sorted(p for p in _walk(pkg, "") if p not in _SPECIAL_FILES)


_ENV_TEMPLATE = "CLAUDE_CODE_OAUTH_TOKEN=\nGH_TOKEN=\n"


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


def _pkg_path(pkg: Traversable, rel: str) -> Traversable:
    src = pkg
    for part in rel.split("/"):
        src = src / part
    return src


def _copy_template(rel: str, target: Path, pkg: Traversable) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    src = _pkg_path(pkg, rel)
    try:
        target.write_bytes(src.read_bytes())
        if target.suffix == ".sh":
            target.chmod(
                target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )
    except Exception as e:
        click.echo(
            click.style(f"Error: could not write {target} — {e}", fg="red"),
            err=True,
        )
        sys.exit(1)


def _merge_env_template(env_file: Path, template: str) -> None:
    """Add any keys from template that are missing from env_file (with empty values)."""
    content = env_file.read_text()
    for line in template.splitlines():
        if not line or "=" not in line:
            continue
        key = line.partition("=")[0].strip()
        if not re.search(rf"^{re.escape(key)}=", content, flags=re.MULTILINE):
            if not content.endswith("\n"):
                content += "\n"
            content += f"{key}=\n"
    env_file.write_text(content)


def _prompt_credential_with_overwrite(
    env_file: Path,
    key: str,
    prompt_text: str,
    existing: dict[str, str],
) -> str:
    """Prompt for a credential, asking for overwrite confirmation if already set."""
    current = existing.get(key, "")
    if current:
        if not click.confirm(f"Overwrite existing {key}?", default=False):
            return current
    return _prompt_and_save_credential(env_file, key, prompt_text)


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


def _refresh_status(rel: str, target: Path, pkg: Traversable) -> str:
    """Return the status verb for copying rel to target without writing."""
    if not target.exists():
        return "created"
    return (
        "unchanged"
        if target.read_bytes() == _pkg_path(pkg, rel).read_bytes()
        else "overwrote"
    )


def refresh() -> None:
    from ..config.loader import load_config

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

    report: list[tuple[str, str]] = []

    for rel in _discover_project_shaped_files(pkg):
        target = project_dir / rel
        verb = _refresh_status(rel, target, pkg)
        _copy_template(rel, target, pkg)
        report.append((verb, rel))

    referenced = referenced_services(load_config())
    dockerfile_template = (
        "Dockerfile.claude-codex" if "codex" in referenced else "Dockerfile.claude"
    )
    dockerfile_target = project_dir / "Dockerfile"
    dockerfile_verb = _refresh_status(dockerfile_template, dockerfile_target, pkg)
    _copy_template(dockerfile_template, dockerfile_target, pkg)
    report.append((dockerfile_verb, "Dockerfile"))

    for path in ("config.py", ".env"):
        if (project_dir / path).exists():
            report.append(("preserved", path))

    overwrote = [(verb, path) for verb, path in report if verb == "overwrote"]
    if overwrote:
        for verb, path in sorted(overwrote, key=lambda x: x[1]):
            print(f"{verb} {path}")
    else:
        print("pycastle directory is already up to date.")


def _role_namespaces() -> list[tuple[AgentRole, str]]:
    pairs: list[tuple[AgentRole, str]] = []
    for role in AgentRole:
        if role == AgentRole.IMPROVE:
            pairs.extend([(role, "main"), (role, "issues")])
        else:
            pairs.append((role, ""))
    return pairs


def _seed_codex_credentials(project_root: Path) -> None:
    host_auth = Path.home() / ".codex" / "auth.json"
    if not host_auth.exists():
        click.echo(
            "No codex credentials found at ~/.codex/auth.json.\n"
            "Install and log in to Codex, then re-run init:\n"
            "1. npm install -g @openai/codex\n"
            "2. codex login\n"
            "3. pycastle init"
        )
        sys.exit(1)

    auth_bytes = host_auth.read_bytes()
    destinations: list[tuple[Path, Path]] = []
    for role, namespace in _role_namespaces():
        base = project_root / SESSION_DIR_NAME / role.value
        codex_dir = (base / namespace if namespace else base) / "codex"
        dest = codex_dir / "auth.json"
        destinations.append((codex_dir, dest))

    if any(dest.exists() for _, dest in destinations):
        if not click.confirm("Overwrite existing codex credentials?", default=False):
            return

    for codex_dir, dest in destinations:
        codex_dir.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(auth_bytes)


def main(scope: Literal["global", "local"] | None = None) -> None:
    project_dir = Path("pycastle")
    pkg = files("pycastle").joinpath("defaults")

    service = click.prompt(
        "Which agent services do you want to use? [claude/codex/both]",
        default="claude",
    )

    if scope is None:
        use_global = click.confirm(
            "Scaffold config.py and .env to global pycastle home? (No = local)",
            default=False,
        )
        scope = "global" if use_global else "local"

    pycastle_home = resolve_global_dir(None, os.environ)
    scoped_dir = pycastle_home if scope == "global" else project_dir

    for rel in _discover_project_shaped_files(pkg):
        target = project_dir / rel
        if target.exists():
            continue
        _copy_template(rel, target, pkg)

    dockerfile_target = project_dir / "Dockerfile"
    if not dockerfile_target.exists():
        dockerfile_template = (
            "Dockerfile.claude" if service == "claude" else "Dockerfile.claude-codex"
        )
        _copy_template(dockerfile_template, dockerfile_target, pkg)

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
        _merge_env_template(env_file, _ENV_TEMPLATE)
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

    existing_env = _read_env_values(env_file)

    gh_token = _prompt_credential_with_overwrite(
        env_file, "GH_TOKEN", "GitHub token (press Enter to skip)", existing_env
    )

    claude_token = ""
    if service != "codex":
        claude_token = _prompt_credential_with_overwrite(
            env_file,
            "CLAUDE_CODE_OAUTH_TOKEN",
            "Claude OAuth token (run `claude setup-token` to generate one; press Enter to skip)",
            existing_env,
        )

        if not claude_token:
            click.echo(
                f"Set CLAUDE_CODE_OAUTH_TOKEN in {env_file} before running pycastle. "
                "Run `claude setup-token` to generate a token."
            )

    click.echo()
    if gh_token and click.confirm("Create GitHub labels?", default=False):
        from .labels import create_labels_interactive

        create_labels_interactive(gh_token)

    click.echo()

    if service in ("codex", "both"):
        _seed_codex_credentials(Path.cwd())
