from __future__ import annotations

import os
import re
import sys
from importlib.resources import files
from pathlib import Path
from typing import Literal

import click

from ..config.loader import (
    derive_docker_image_name,
    resolve_global_dir,
)
from ..scaffold import InitScaffold

_ENV_TEMPLATE = "CLAUDE_CODE_OAUTH_TOKEN=\nGH_TOKEN=\n"
_OPENCODE_ENV_TEMPLATE = "OPENCODE_GO_API_KEY=\n"
_SUPPORTED_SERVICE_SELECTIONS: dict[str, tuple[str, ...]] = {
    "claude": ("claude",),
    "codex": ("codex",),
    "opencode": ("opencode",),
    "all": ("claude", "codex", "opencode"),
}


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


def _parse_service_selection(selection: str) -> tuple[str, ...]:
    normalized = selection.strip().lower() or "all"
    service_set = _SUPPORTED_SERVICE_SELECTIONS.get(normalized)
    if service_set is None:
        choices = "/".join(_SUPPORTED_SERVICE_SELECTIONS)
        raise click.ClickException(
            f"Invalid service selection {selection!r}. Choose one of: {choices}."
        )
    return service_set


def _managed_env_template(service_set: tuple[str, ...]) -> str:
    template = _ENV_TEMPLATE
    if "opencode" in service_set:
        template += _OPENCODE_ENV_TEMPLATE
    return template


def _selected_services_cover_bundled_default_stage_chains(
    service_set: tuple[str, ...],
    scaffold: InitScaffold,
) -> bool:
    selected = set(service_set)
    return all(
        any(service in selected for service in stage)
        for stage in scaffold.bundled_default_stage_chains()
    )


def _warn_for_uncovered_bundled_default_stage_chains(
    service_set: tuple[str, ...],
    scaffold: InitScaffold,
) -> None:
    if _selected_services_cover_bundled_default_stage_chains(service_set, scaffold):
        return
    click.echo(
        "Warning: selected services do not cover every bundled default stage "
        "priority chain. Define your own stage overrides in config.py before "
        "running pycastle."
    )


def _warn_for_missing_host_codex_auth(service_set: tuple[str, ...]) -> None:
    if "codex" not in service_set:
        return
    if (Path.home() / ".codex" / "auth.json").exists():
        return
    click.echo("Warning: Codex authentication missing: run `codex login` on the host.")


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


def refresh() -> None:
    project_dir = Path("pycastle")
    pkg = files("pycastle").joinpath("defaults")
    pycastle_home = resolve_global_dir(None, os.environ)
    scaffold = InitScaffold(
        pycastle_dir=project_dir,
        pycastle_home=pycastle_home,
        defaults=pkg,
    )

    try:
        report = scaffold.refresh()
    except Exception as e:
        click.echo(
            click.style(f"Error: could not refresh pycastle scaffold — {e}", fg="red"),
            err=True,
        )
        sys.exit(1)

    for line in report.display_lines():
        print(line)


def main(scope: Literal["global", "local"] | None = None) -> None:
    project_dir = Path("pycastle")
    pkg = files("pycastle").joinpath("defaults")
    scaffold = InitScaffold(
        pycastle_dir=project_dir,
        pycastle_home=resolve_global_dir(None, os.environ),
        defaults=pkg,
    )

    service_selection = click.prompt(
        "Which agent services do you want to use? [claude/codex/opencode/all]",
        default="all",
    )
    service_set = _parse_service_selection(service_selection)
    _warn_for_uncovered_bundled_default_stage_chains(service_set, scaffold)
    _warn_for_missing_host_codex_auth(service_set)

    if scope is None:
        use_global = click.confirm(
            "Scaffold config.py and .env to global pycastle home? (No = local)",
            default=False,
        )
        scope = "global" if use_global else "local"

    pycastle_home = scaffold.pycastle_home
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

    try:
        scaffold.refresh()
    except Exception as e:
        click.echo(
            click.style(f"Error: could not write pycastle scaffold — {e}", fg="red"),
            err=True,
        )
        sys.exit(1)

    config_file = scoped_dir / "config.py"
    if config_file.exists():
        if scope == "global":
            click.echo(
                f"global config.py already exists at {config_file}; leaving it untouched"
            )
    else:
        try:
            config_file.parent.mkdir(parents=True, exist_ok=True)
            config_file.write_bytes((pkg / "config.py").read_bytes())
        except Exception as e:
            click.echo(
                click.style(f"Error: could not write {config_file} — {e}", fg="red"),
                err=True,
            )
            sys.exit(1)

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
        env_template = _managed_env_template(service_set)
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

        if "claude" in service_set:
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

        if "opencode" in service_set:
            _prompt_credential_with_overwrite(
                env_file,
                "OPENCODE_GO_API_KEY",
                "OpenCode Go API key (press Enter to skip)",
                existing_env,
            )

    click.echo()
    if gh_token and click.confirm("Create GitHub labels?", default=False):
        from .labels import create_labels_interactive

        create_labels_interactive(gh_token)

    click.echo()
