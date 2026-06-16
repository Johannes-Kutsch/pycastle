from __future__ import annotations

import re
import sys
from importlib.resources import files
from pathlib import Path
from typing import Literal

import click

from ..init_wizard import (
    build_init_plan_for_scope,
    ConfigFileAction,
    HostAuthFacts,
    InitPlan,
    ScaffoldStageChainFacts,
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


def _echo_init_plan_warnings(plan: InitPlan) -> None:
    for message in plan.warning_messages():
        click.echo(f"Warning: {message}")


def _apply_config_file_action(defaults_pkg, config_action: ConfigFileAction) -> None:
    config_file = config_action.path
    if not config_action.should_create:
        if config_action.message is not None:
            click.echo(config_action.message)
    else:
        try:
            config_file.parent.mkdir(parents=True, exist_ok=True)
            config_file.write_bytes((defaults_pkg / "config.py").read_bytes())
        except Exception as e:
            click.echo(
                click.style(f"Error: could not write {config_file} — {e}", fg="red"),
                err=True,
            )
            sys.exit(1)

    for hint in config_action.hints:
        try:
            _fill_commented_hint(config_file, hint.key, hint.value)
        except Exception as e:
            click.echo(
                click.style(
                    f"Error: could not set {hint.key} in {config_file} — {e}",
                    fg="red",
                ),
                err=True,
            )
            sys.exit(1)


def _prompt_credential_with_overwrite(
    env_file: Path,
    key: str,
    prompt_text: str,
    existing: dict[str, str],
) -> tuple[str, bool]:
    """Prompt for a credential, asking for overwrite confirmation if already set."""
    current = existing.get(key, "")
    if current:
        if not click.confirm(f"Overwrite existing {key}?", default=False):
            return current, False
    return _prompt_and_save_credential(env_file, key, prompt_text), True


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

    def plan_for_scope(
        plan_scope: Literal["global", "local"],
        manage_env_file: bool = False,
        prompted_env_values: dict[str, str] | None = None,
        existing_env_keys: tuple[str, ...] = (),
        existing_env_values: dict[str, str] | None = None,
        target_env_exists: bool | None = None,
        local_env_exists: bool | None = None,
        global_env_exists: bool | None = None,
        host_auth: HostAuthFacts | None = None,
        scaffold_stage_chains: ScaffoldStageChainFacts | None = None,
    ) -> InitPlan:
        try:
            return build_init_plan_for_scope(
                selected_services=(service_selection,),
                scope_choice=plan_scope,
                pycastle_dir=layout.pycastle_dir,
                pycastle_home=layout.pycastle_home,
                manage_env_file=manage_env_file,
                prompted_env_values=prompted_env_values,
                existing_env_keys=existing_env_keys,
                existing_env_values=existing_env_values,
                target_env_exists=target_env_exists,
                local_env_exists=local_env_exists,
                global_env_exists=global_env_exists,
                host_auth=host_auth,
                scaffold_stage_chains=scaffold_stage_chains,
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

    service_selection = click.prompt(
        "Which agent services do you want to use? [claude/codex/opencode/all]",
        default="all",
    )
    service_plan = plan_for_scope(
        "local",
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
    init_plan = plan_for_scope(
        scope,
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
        init_plan = plan_for_scope(
            scope,
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

    config_action = init_plan.config_file_action
    if config_action is not None:
        _apply_config_file_action(pkg, config_action)

    env_file = init_plan.planned_env_file.path
    gh_token = ""
    claude_token = ""
    if manage_env_file:
        if env_file.exists():
            existing_env_keys = _read_env_keys(env_file)
            env_plan = plan_for_scope(
                scope,
                existing_env_keys=existing_env_keys,
                existing_env_values=_read_env_values(env_file),
                target_env_exists=True,
                local_env_exists=local_env_exists,
                global_env_exists=global_env_exists,
            )
            _merge_missing_env_keys(env_file, env_plan.planned_env_file.missing_keys)
        else:
            env_plan = plan_for_scope(
                scope,
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
        env_plan = plan_for_scope(
            scope,
            existing_env_keys=_read_env_keys(env_file),
            existing_env_values=existing_env,
            target_env_exists=True,
            local_env_exists=local_env_exists or env_file == layout.local_env_file,
            global_env_exists=global_env_exists or env_file == layout.global_env_file,
        )

        prompted_values: dict[str, str] = {}
        effective_values: dict[str, str] = {}
        for credential_prompt in env_plan.credential_prompts:
            if credential_prompt.allow_overwrite:
                value, was_prompted = _prompt_credential_with_overwrite(
                    env_file,
                    credential_prompt.key,
                    credential_prompt.prompt_text,
                    existing_env,
                )
                if was_prompted and value:
                    prompted_values[credential_prompt.key] = value
            else:
                value = _prompt_and_save_credential(
                    env_file, credential_prompt.key, credential_prompt.prompt_text
                )
                if value:
                    prompted_values[credential_prompt.key] = value
            effective_values[credential_prompt.key] = value

        gh_token = effective_values.get("GH_TOKEN", "")
        claude_token = effective_values.get("CLAUDE_CODE_OAUTH_TOKEN", "")

        if "claude" in env_plan.selected_services and not claude_token:
            click.echo(
                f"Set CLAUDE_CODE_OAUTH_TOKEN in {env_file} before running pycastle. "
                "Run `claude setup-token` to generate a token."
            )

    click.echo()
    label_plan = plan_for_scope(
        scope,
        manage_env_file=manage_env_file,
        prompted_env_values=prompted_values if manage_env_file else {},
        target_env_exists=env_file.exists(),
        local_env_exists=layout.local_env_file.exists(),
        global_env_exists=layout.global_env_file.exists(),
    )
    if label_plan.label_prompt_eligibility.should_prompt and click.confirm(
        "Create GitHub labels?", default=False
    ):
        from .labels import create_labels_interactive

        create_labels_interactive(gh_token)

    click.echo()
