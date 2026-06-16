from __future__ import annotations

import re
import sys
from importlib.resources import files
from pathlib import Path
from typing import Literal

import click

from ..config.loader import derive_docker_image_name
from ..init_wizard import (
    HostAuthFacts,
    InitPlan,
    InitWizardLayoutFacts,
    InitWizardPlanningInputs,
    ScaffoldStageChainFacts,
    build_init_plan,
)
from ..layout import resolve_layout
from ..scaffold import InitScaffold


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


def _read_env_keys(env_file: Path) -> tuple[str, ...]:
    if not env_file.exists():
        return ()
    keys: list[str] = []
    for line in env_file.read_text().splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key = line.partition("=")[0].strip()
        if key:
            keys.append(key)
    return tuple(keys)


def _merge_missing_env_keys(env_file: Path, missing_keys: tuple[str, ...]) -> None:
    """Add any missing keys to env_file with empty values."""
    content = env_file.read_text()
    for key in missing_keys:
        if not content.endswith("\n"):
            content += "\n"
        content += f"{key}=\n"
    env_file.write_text(content)


def _plan_layout(
    layout,
    scope: Literal["global", "local"],
) -> InitWizardLayoutFacts:
    scoped_dir = layout.pycastle_home if scope == "global" else layout.pycastle_dir
    return InitWizardLayoutFacts(
        pycastle_dir=layout.pycastle_dir,
        pycastle_home=layout.pycastle_home,
        target_config_file=scoped_dir / "config.py",
        target_env_file=scoped_dir / ".env",
        local_env_file=layout.local_env_file,
        global_env_file=layout.global_env_file,
    )


def _build_click_init_plan(
    *,
    layout,
    scope: Literal["global", "local"],
    service_selection: str,
    existing_env_keys: tuple[str, ...] = (),
    existing_env_values: dict[str, str] | None = None,
    target_env_exists: bool | None = None,
    local_env_exists: bool | None = None,
    global_env_exists: bool | None = None,
    host_auth: HostAuthFacts | None = None,
    scaffold_stage_chains: ScaffoldStageChainFacts | None = None,
):
    try:
        return build_init_plan(
            InitWizardPlanningInputs(
                selected_services=(service_selection,),
                scope_choice=scope,
                layout=_plan_layout(layout, scope),
                existing_env_keys=existing_env_keys,
                existing_env_values=(
                    {} if existing_env_values is None else existing_env_values
                ),
                target_env_exists=target_env_exists,
                local_env_exists=local_env_exists,
                global_env_exists=global_env_exists,
                host_auth=host_auth or HostAuthFacts(False),
                scaffold_stage_chains=(
                    scaffold_stage_chains or ScaffoldStageChainFacts()
                ),
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


def _echo_init_plan_warnings(plan: InitPlan) -> None:
    for message in plan.warning_messages():
        click.echo(f"Warning: {message}")


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
    layout = resolve_layout()
    pkg = files("pycastle").joinpath("defaults")
    scaffold = InitScaffold(
        pycastle_dir=layout.pycastle_dir,
        pycastle_home=layout.pycastle_home,
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
    layout = resolve_layout()
    pkg = files("pycastle").joinpath("defaults")
    scaffold = InitScaffold(
        pycastle_dir=layout.pycastle_dir,
        pycastle_home=layout.pycastle_home,
        defaults=pkg,
    )

    service_selection = click.prompt(
        "Which agent services do you want to use? [claude/codex/opencode/all]",
        default="all",
    )
    service_plan = _build_click_init_plan(
        layout=layout,
        scope="local",
        service_selection=service_selection,
        target_env_exists=layout.local_env_file.exists(),
        local_env_exists=layout.local_env_file.exists(),
        global_env_exists=layout.global_env_file.exists(),
        host_auth=HostAuthFacts(
            has_host_codex_auth=(Path.home() / ".codex" / "auth.json").exists()
        ),
        scaffold_stage_chains=ScaffoldStageChainFacts(
            bundled_default_stage_chains=scaffold.bundled_default_stage_chains()
        ),
    )
    _echo_init_plan_warnings(service_plan)

    if scope is None:
        use_global = click.confirm(
            "Scaffold config.py and .env to global pycastle home? (No = local)",
            default=False,
        )
        scope = "global" if use_global else "local"

    local_env_file = layout.local_env_file
    local_env_exists = local_env_file.exists()
    global_env_exists = layout.global_env_file.exists()
    init_plan = _build_click_init_plan(
        layout=layout,
        scope=scope,
        service_selection=service_selection,
        target_env_exists=(
            local_env_exists if scope == "local" else layout.global_env_file.exists()
        ),
        local_env_exists=local_env_exists,
        global_env_exists=global_env_exists,
    )
    manage_env_file = init_plan.planned_env_file.should_manage

    if init_plan.planned_env_file.should_delete_local_env:
        if click.confirm(
            "Delete local .env? (Global will be used instead)", default=False
        ):
            local_env_file.unlink()
            local_env_exists = False
    if init_plan.planned_env_file.should_create_local_env:
        manage_env_file = click.confirm(
            "Create local .env? (Global stays unchanged, local takes priority)",
            default=False,
        )
    if local_env_exists != layout.local_env_file.exists():
        init_plan = _build_click_init_plan(
            layout=layout,
            scope=scope,
            service_selection=service_selection,
            target_env_exists=(
                local_env_exists
                if scope == "local"
                else layout.global_env_file.exists()
            ),
            local_env_exists=local_env_exists,
            global_env_exists=global_env_exists,
        )

    try:
        scaffold.refresh()
    except Exception as e:
        click.echo(
            click.style(f"Error: could not write pycastle scaffold — {e}", fg="red"),
            err=True,
        )
        sys.exit(1)

    config_file = init_plan.target_config_file
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

    env_file = init_plan.planned_env_file.path
    gh_token = ""
    claude_token = ""
    if manage_env_file:
        if env_file.exists():
            existing_env_keys = _read_env_keys(env_file)
            env_plan = _build_click_init_plan(
                layout=layout,
                scope=scope,
                service_selection=service_selection,
                existing_env_keys=existing_env_keys,
                existing_env_values=_read_env_values(env_file),
                target_env_exists=True,
                local_env_exists=local_env_exists,
                global_env_exists=global_env_exists,
            )
            _merge_missing_env_keys(env_file, env_plan.planned_env_file.missing_keys)
        else:
            env_plan = _build_click_init_plan(
                layout=layout,
                scope=scope,
                service_selection=service_selection,
                target_env_exists=False,
                local_env_exists=local_env_exists,
                global_env_exists=global_env_exists,
            )
            env_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                env_file.write_text(
                    "".join(
                        f"{key}=\n" for key in env_plan.planned_env_file.missing_keys
                    )
                )
            except Exception as e:
                click.echo(
                    click.style(f"Error: could not write {env_file} — {e}", fg="red"),
                    err=True,
                )
                sys.exit(1)

        existing_env = _read_env_values(env_file)
        env_plan = _build_click_init_plan(
            layout=layout,
            scope=scope,
            service_selection=service_selection,
            existing_env_keys=_read_env_keys(env_file),
            existing_env_values=existing_env,
            target_env_exists=True,
            local_env_exists=local_env_exists or env_file == layout.local_env_file,
            global_env_exists=global_env_exists or env_file == layout.global_env_file,
        )

        prompted_values: dict[str, str] = {}
        for credential_prompt in env_plan.credential_prompts:
            if credential_prompt.allow_overwrite:
                value = _prompt_credential_with_overwrite(
                    env_file,
                    credential_prompt.key,
                    credential_prompt.prompt_text,
                    existing_env,
                )
            else:
                value = _prompt_and_save_credential(
                    env_file, credential_prompt.key, credential_prompt.prompt_text
                )
            prompted_values[credential_prompt.key] = value

        gh_token = prompted_values.get("GH_TOKEN", "")
        claude_token = prompted_values.get("CLAUDE_CODE_OAUTH_TOKEN", "")

        if "claude" in env_plan.selected_services and not claude_token:
            click.echo(
                f"Set CLAUDE_CODE_OAUTH_TOKEN in {env_file} before running pycastle. "
                "Run `claude setup-token` to generate a token."
            )

    click.echo()
    if gh_token and click.confirm("Create GitHub labels?", default=False):
        from .labels import create_labels_interactive

        create_labels_interactive(gh_token)

    click.echo()
