#!/usr/bin/env python3
import asyncio
import os
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

from .config import Config, load_config
from .errors import ClaudeCliNotFoundError, ConfigValidationError

_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "GH_TOKEN",
)


def _load_env(cfg: Config | None = None) -> dict[str, str]:
    if cfg is None:
        cfg = load_config()
    load_dotenv(cfg.env_file)
    env = {k: v for k in _ENV_KEYS if (v := os.getenv(k))}
    claude_json = Path.home() / ".claude.json"
    if claude_json.exists():
        env["CLAUDE_ACCOUNT_JSON"] = claude_json.read_text(encoding="utf-8")
    else:
        print(
            "Warning: ~/.claude.json not found — container will run without account credentials.",
            file=sys.stderr,
        )
    return env


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
def init_cmd() -> None:
    from .init_command import main as _init

    _init()


@main.command("labels")
def labels_cmd() -> None:
    from .labels import main as _labels

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

    cfg = _load_config_or_exit()
    _build(no_cache, cfg=cfg)


@main.command("run")
def run_cmd() -> None:
    from .orchestrator import run

    cfg = _load_config_or_exit()
    asyncio.run(run(_load_env(cfg=cfg), Path(".").resolve()))


if __name__ == "__main__":
    main()
