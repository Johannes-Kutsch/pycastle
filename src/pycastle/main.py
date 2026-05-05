#!/usr/bin/env python3
import asyncio
import os
import sys
from pathlib import Path
from typing import Literal

import click

from .config import Config, load_config, load_env, resolve_global_dir
from .config.loader import describe_config_layers
from .errors import ClaudeCliNotFoundError, ConfigValidationError
from .status_display import PlainStatusDisplay

_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "GH_TOKEN",
)


def _load_env(cfg: Config | None = None) -> dict[str, str]:
    if cfg is None:
        cfg = load_config()
    resolved = load_env(
        global_dir=resolve_global_dir(None, os.environ),
        local_env_file=cfg.env_file,
        process_env=os.environ,
    )
    env = {k: v for k in _ENV_KEYS if (v := resolved.get(k))}
    claude_json = Path.home() / ".claude.json"
    if claude_json.exists():
        env["CLAUDE_ACCOUNT_JSON"] = claude_json.read_text(encoding="utf-8")
    else:
        print(
            "Warning: ~/.claude.json not found — container will run without account credentials.",
            file=sys.stderr,
        )
    return env


def _print_layer_summary() -> None:
    summary = describe_config_layers()
    PlainStatusDisplay().print("", summary)


def _load_config_or_exit() -> Config:
    try:
        return load_config()
    except ClaudeCliNotFoundError:
        click.echo(
            "Claude CLI not found. Install it with: sudo npm install -g @anthropic-ai/claude-code",
            err=True,
        )
        sys.exit(1)
    except ConfigValidationError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)


@click.group()
@click.version_option(package_name="pycastle", prog_name="pycastle")
def main() -> None:
    pass


@main.command("init")
@click.option(
    "--global",
    "global_flag",
    is_flag=True,
    default=False,
    help="Scaffold config.py and .env to pycastle home (~/.config/pycastle/).",
)
@click.option(
    "--local",
    "local_flag",
    is_flag=True,
    default=False,
    help="Scaffold config.py and .env locally to ./pycastle/.",
)
@click.option(
    "--refresh",
    "refresh_flag",
    is_flag=True,
    default=False,
    help="Re-pull bundled project-shaped files (Dockerfile, .gitignore, prompts/**) "
    "into ./pycastle/ without prompts. Leaves config.py and .env untouched.",
)
def init_cmd(global_flag: bool, local_flag: bool, refresh_flag: bool) -> None:
    from .init_command import main as _init
    from .init_command import refresh as _refresh

    _print_layer_summary()
    if refresh_flag and (global_flag or local_flag):
        click.echo(
            "Error: --refresh is mutually exclusive with --global and --local.",
            err=True,
        )
        sys.exit(1)
    if refresh_flag:
        _refresh()
        return
    if global_flag and local_flag:
        click.echo("Error: --global and --local are mutually exclusive.", err=True)
        sys.exit(1)
    scope: Literal["global", "local"] | None
    if global_flag:
        scope = "global"
    elif local_flag:
        scope = "local"
    else:
        scope = None
    _init(scope=scope)


@main.command("labels")
def labels_cmd() -> None:
    from .labels import main as _labels

    _print_layer_summary()
    cfg = _load_config_or_exit()
    _labels(cfg=cfg)


@main.command("build")
@click.option(
    "--no-cache",
    is_flag=True,
    default=False,
    help="Build without using the Docker cache.",
)
def build_cmd(no_cache: bool) -> None:
    from .build_command import main as _build

    _print_layer_summary()
    cfg = _load_config_or_exit()
    _build(no_cache, cfg=cfg)


@main.command("run")
def run_cmd() -> None:
    from .orchestrator import run

    _print_layer_summary()
    cfg = _load_config_or_exit()
    asyncio.run(run(_load_env(cfg=cfg), Path(".").resolve()))


if __name__ == "__main__":
    main()
