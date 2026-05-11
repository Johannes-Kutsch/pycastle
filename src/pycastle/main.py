#!/usr/bin/env python3
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Literal

import click

from .config import Config, load_config, load_env, resolve_global_dir
from .config.loader import describe_config_layers
from .errors import ClaudeCliNotFoundError, ConfigValidationError
from .status_display import PlainStatusDisplay

_ENV_KEYS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY",
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
    return {k: v for k in _ENV_KEYS if (v := resolved.get(k))}


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


class _BugReportingGroup(click.Group):
    """Click group that funnels unhandled exceptions through the bug reporter.

    Click's own flow-control exceptions (`ClickException`, `Abort`, `Exit`) and
    `SystemExit` / `KeyboardInterrupt` pass through unchanged so click's normal
    error handling and signal semantics are preserved.
    """

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except (click.ClickException, click.exceptions.Exit, click.Abort):
            raise
        except Exception as exc:
            from .bug_reporter import report_and_exit

            report_and_exit(exc)


@click.group(cls=_BugReportingGroup)
@click.version_option(package_name="pycastle", prog_name="pycastle")
def main() -> None:
    from ._shutdown_hook import install_urllib3_shutdown_hook

    install_urllib3_shutdown_hook()


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
    if refresh_flag:
        if global_flag or local_flag:
            click.echo(
                "Error: --refresh is mutually exclusive with --global and --local.",
                err=True,
            )
            sys.exit(1)
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
@click.option(
    "--improve",
    "improve_mode",
    default=None,
    is_flag=False,
    flag_value="until_sleep",
    type=click.Choice(["until_sleep", "endless"]),
    help=(
        "Dispatch the improve agent when no issues are ready. "
        "Bare --improve defaults to 'until_sleep' (exits after first sleep clears backlog). "
        "'endless' keeps generating until Ctrl-C."
    ),
)
def run_cmd(improve_mode: str | None) -> None:
    from typing import cast

    from .account_pool import AccountPool
    from .iteration.dispatcher import ImproveMode
    from .orchestrator import run

    _print_layer_summary()
    cfg = _load_config_or_exit()
    env = _load_env(cfg=cfg)
    primary = env.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not primary:
        click.echo(
            "Error: CLAUDE_CODE_OAUTH_TOKEN is not set. "
            "Run `claude setup-token` to generate a token, then add it to your .env file.",
            err=True,
        )
        sys.exit(1)
    accounts: list[tuple[str, str]] = []
    secondary = env.get("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY")
    if secondary:
        accounts.append(("secondary", secondary))
    accounts.append(("primary", primary))
    pool = AccountPool(accounts)
    # Strip the secondary token from container env; AgentRunner injects the
    # pool-picked token into CLAUDE_CODE_OAUTH_TOKEN per session.
    container_env = {
        k: v for k, v in env.items() if k != "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY"
    }
    effective_improve_mode = (
        improve_mode if improve_mode is not None else cfg.improve_mode
    )
    asyncio.run(
        run(
            container_env,
            Path(".").resolve(),
            account_pool=pool,
            improve_mode=cast(ImproveMode, effective_improve_mode),
        )
    )


if __name__ == "__main__":
    main()
