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

from ..config.loader import (
    derive_docker_image_name,
    resolve_global_dir,
)

_INIT_REFRESHED_FILES = {
    ".gitignore",
    "setup/cron.sh",
    "setup/cron-install.sh",
    "setup/cron-uninstall.sh",
}

_ENV_TEMPLATE = "CLAUDE_CODE_OAUTH_TOKEN=\nGH_TOKEN=\n"
_OPENCODE_ENV_TEMPLATE = "OPENCODE_GO_API_KEY=\n"

_CONFIG_FIELD_RE = re.compile(r"[a-z_]+\s*=")


def _render_config_example(defaults_text: str) -> str:
    out = ["from pathlib import Path", ""]
    uncomment_block = False
    preserve_commented_block = False

    for line in defaults_text.splitlines():
        if line == "from pathlib import Path":
            continue
        if line.startswith("# "):
            body = line[2:]
            if uncomment_block:
                out.append(body)
                if body.strip() == ")":
                    uncomment_block = False
                continue
            if preserve_commented_block:
                out.append(line)
                if body.strip() == ")":
                    preserve_commented_block = False
                continue
            if _CONFIG_FIELD_RE.match(body):
                if body.startswith("opencode_"):
                    out.append(line)
                    preserve_commented_block = body.rstrip().endswith("(")
                else:
                    out.append(body)
                    uncomment_block = body.rstrip().endswith("(")
                continue
        out.append(line)

    return "\n".join(out).rstrip() + "\n"


def _load_config_example_template(pkg: Traversable) -> str:
    return _render_config_example((pkg / "config.py").read_text())


_CONFIG_EXAMPLE_TEMPLATE = _load_config_example_template(
    files("pycastle").joinpath("defaults")
)


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


def _managed_env_template(service: str) -> str:
    if service == "opencode":
        return _ENV_TEMPLATE + _OPENCODE_ENV_TEMPLATE
    return _ENV_TEMPLATE


def _write_config_example(target_dir: Path, content: str) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "config.py.example").write_text(content)


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


def _refresh_status_text(target: Path, expected: str) -> str:
    """Return the status verb for writing expected text to target."""
    if not target.exists():
        return "created"
    return "unchanged" if target.read_text() == expected else "overwrote"


def refresh() -> None:
    project_dir = Path("pycastle")
    project_dir.mkdir(parents=True, exist_ok=True)
    pkg = files("pycastle").joinpath("defaults")
    config_example_template = _load_config_example_template(pkg)
    pycastle_home = resolve_global_dir(None, os.environ)
    config_example_path = project_dir / "config.py.example"
    config_example_verb = _refresh_status_text(
        config_example_path, config_example_template
    )
    _write_config_example(project_dir, config_example_template)
    if (pycastle_home / "config.py.example").exists():
        _write_config_example(pycastle_home, config_example_template)

    report: list[tuple[str, str]] = [(config_example_verb, "config.py.example")]

    for rel in sorted(_INIT_REFRESHED_FILES):
        target = project_dir / rel
        verb = _refresh_status(rel, target, pkg)
        _copy_template(rel, target, pkg)
        report.append((verb, rel))

    for path in ("config.py", ".env"):
        if (project_dir / path).exists():
            report.append(("preserved", path))

    overwrote = [(verb, path) for verb, path in report if verb == "overwrote"]
    created = any(verb == "created" for verb, _ in report)
    if overwrote:
        for verb, path in sorted(overwrote, key=lambda x: x[1]):
            print(f"{verb} {path}")
    elif not created:
        print("pycastle directory is already up to date.")


def main(scope: Literal["global", "local"] | None = None) -> None:
    project_dir = Path("pycastle")
    pkg = files("pycastle").joinpath("defaults")

    service = click.prompt(
        "Which agent services do you want to use? [claude/codex/opencode/both]",
        default="claude",
    )
    if service not in {"claude", "codex", "opencode", "both"}:
        service = "claude"

    if scope is None:
        use_global = click.confirm(
            "Scaffold config.py and .env to global pycastle home? (No = local)",
            default=False,
        )
        scope = "global" if use_global else "local"

    pycastle_home = resolve_global_dir(None, os.environ)
    scoped_dir = pycastle_home if scope == "global" else project_dir
    local_env_file = project_dir / ".env"
    manage_env_file = True

    if scope == "global" and local_env_file.exists():
        if click.confirm(
            "Delete local .env? (Global will be used instead)", default=False
        ):
            local_env_file.unlink()
    if (
        scope == "local"
        and (pycastle_home / ".env").exists()
        and not local_env_file.exists()
    ):
        manage_env_file = click.confirm(
            "Create local .env? (Global stays unchanged, local takes priority)",
            default=False,
        )

    for rel in sorted(_INIT_REFRESHED_FILES):
        target = project_dir / rel
        _copy_template(rel, target, pkg)

    config_example_template = _load_config_example_template(pkg)
    _write_config_example(project_dir, config_example_template)
    if pycastle_home.is_dir():
        _write_config_example(pycastle_home, config_example_template)

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
    gh_token = ""
    claude_token = ""
    if manage_env_file:
        env_template = _managed_env_template(service)
        if env_file.exists():
            _merge_env_template(env_file, env_template)
        else:
            env_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                env_file.write_text(env_template)
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

        if service == "opencode":
            _prompt_credential_with_overwrite(
                env_file,
                "OPENCODE_GO_API_KEY",
                "OpenCode Go API key (press Enter to skip)",
                existing_env,
            )
        elif service != "codex":
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
