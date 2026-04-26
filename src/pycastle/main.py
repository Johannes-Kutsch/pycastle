#!/usr/bin/env python3
import asyncio
import os
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

from .config import ENV_FILE

_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "GH_TOKEN",
)


def _load_env() -> dict[str, str]:
    load_dotenv(ENV_FILE)
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


@click.group()
def main() -> None:
    pass


@main.command("init")
def init_cmd() -> None:
    from .init_command import main as _init

    _init()


@main.command("labels")
def labels_cmd() -> None:
    from .labels import main as _labels

    _labels()


@main.command("build")
@click.option(
    "--no-cache",
    is_flag=True,
    default=False,
    help="Build without using the Docker cache.",
)
def build_cmd(no_cache: bool) -> None:
    from .build_command import main as _build

    _build(no_cache)


@main.command("run")
def run_cmd() -> None:
    from .orchestrator import run

    asyncio.run(run(_load_env(), Path(".").resolve()))


if __name__ == "__main__":
    main()
