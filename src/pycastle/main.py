#!/usr/bin/env python3
# ruff: noqa: EXE001  # installed as a console_scripts entry point, not executed directly
import asyncio
import sys
from pathlib import Path
from typing import Any, Literal

import click

from pycastle import orchestration as pycastle_orchestration
from pycastle._universal_image_build import UniversalImageBuildOptions
from pycastle.config import Config, load_config, load_credential_env, resolve_logs_dir
from pycastle.display.status_display import PlainStatusDisplay
from pycastle.errors import (
    ClaudeCliNotFoundError,
    ConfigValidationError,
    DockerServiceError,
)
from pycastle.layout import describe_config_layers, resolve_layout
from pycastle.run_startup_preparation import (
    RunStartupImproveModeFlagFacts,
    prepare_run_startup,
)
from pycastle.services.service_registry import ServiceRegistry
from pycastle.stage_priority_chain import (
    chain_entries,
    render_chain_label,
    validation_labels,
)


class _AgentRuntimeAdapter:
    def __init__(self) -> None:
        self.ServiceRegistry = ServiceRegistry
        self.chain_entries = chain_entries
        self.render_chain_label = render_chain_label
        self.validation_labels = validation_labels

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401  # dynamic attribute lookup on adapter; type depends on the attribute name
        if name == "run":
            return pycastle_orchestration.run
        raise AttributeError(name)


agent_runtime: Any = _AgentRuntimeAdapter()


def _load_env(cfg: Config | None = None) -> dict[str, str]:
    if cfg is None:
        load_config()
    return load_credential_env()


def _print_layer_summary() -> None:
    summary = describe_config_layers()
    PlainStatusDisplay().print("", summary)


def _check_pycastle_dir_or_exit() -> None:
    layout = resolve_layout()
    if not layout.pycastle_dir.is_dir():
        click.echo(
            f"Error: no pycastle/ directory found in {layout.repo_root} — "
            "this project isn't initialized.\n"
            "Run `pycastle init` from your repo root, "
            "or cd to the repo root before running pycastle.",
            err=True,
        )
        sys.exit(1)


def _load_config_or_exit() -> Config:
    _check_pycastle_dir_or_exit()
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

    def invoke(self, ctx: click.Context) -> Any:  # noqa: ANN401  # overrides click.Group.invoke which itself returns Any
        try:
            return super().invoke(ctx)
        except (click.ClickException, click.exceptions.Exit, click.Abort):
            raise
        except Exception as exc:
            from pycastle.bug_reporter import report_and_exit

            report_and_exit(exc)
            raise


@click.group(cls=_BugReportingGroup)
@click.version_option(package_name="pycastle", prog_name="pycastle")
def main() -> None:
    from pycastle.infrastructure.shutdown_hook import install_urllib3_shutdown_hook

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
    help="Re-pull bundled pycastle-managed scaffold files into ./pycastle/ "
    "without prompts. Leaves config.py and .env untouched.",
)
def init_cmd(*, global_flag: bool, local_flag: bool, refresh_flag: bool) -> None:
    from pycastle.commands.init import main as _init
    from pycastle.commands.init import refresh as _refresh

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
    from pycastle.commands.labels import main as _labels

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
def build_cmd(*, no_cache: bool) -> None:
    from pycastle.commands.build import main as _build

    _print_layer_summary()
    cfg = _load_config_or_exit()
    try:
        _build(options=UniversalImageBuildOptions(no_cache=no_cache), cfg=cfg)
    except (ConfigValidationError, DockerServiceError) as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)


@main.command("check")
def check_cmd() -> None:
    from pycastle.commands.check import main as _check

    _print_layer_summary()
    cfg = _load_config_or_exit()
    try:
        _check(cfg=cfg)
    except RuntimeError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)


def _do_run(
    cfg: Config,
    *,
    no_improve: bool,
    improve_mode_flag: str | None,
) -> None:
    from typing import cast

    from pycastle.commands.build import main as _build
    from pycastle.run_startup_preparation import RunImproveMode

    startup = prepare_run_startup(
        cfg,
        _load_env(cfg=cfg),
        RunStartupImproveModeFlagFacts(
            no_improve=no_improve,
            improve_mode_flag=cast("RunImproveMode", improve_mode_flag),
        ),
    )
    if startup.validation_error_message is not None:
        click.echo(startup.validation_error_message, err=True)
        sys.exit(1)

    try:
        _build(
            options=UniversalImageBuildOptions(stream=True, terse=True),
            cfg=cfg,
        )
    except (ConfigValidationError, DockerServiceError) as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    asyncio.run(
        agent_runtime.run(
            startup.shared_container_env,
            Path.cwd(),
            service_registry=startup.runtime_registry,
            improve_mode=startup.effective_improve_mode,
        )
    )


_IMPROVE_OPTION = click.option(
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
_NO_IMPROVE_OPTION = click.option(
    "--no-improve",
    "no_improve",
    is_flag=True,
    default=False,
    help="Disable improve-agent dispatch for this run, overriding any improve_mode in config.",
)


_IGNORE_GLOBAL_LOCK_OPTION = click.option(
    "--ignore-global-lock",
    "ignore_global_lock",
    is_flag=True,
    default=False,
    help=(
        "Queue-jumping run: skip the host-wide global run lock and start immediately "
        "even while another project's run is in flight. "
        "Still acquires this project's run marker, so a second run of the same "
        "project is rejected as usual."
    ),
)


@main.command("run")
@_IMPROVE_OPTION
@_NO_IMPROVE_OPTION
@_IGNORE_GLOBAL_LOCK_OPTION
def run_cmd(
    *, improve_mode: str | None, no_improve: bool, ignore_global_lock: bool
) -> None:
    from pycastle.commands.init import refresh as _refresh
    from pycastle.errors import RunAlreadyInProgressError, RunSlotTimeoutError
    from pycastle.log_maintenance import maintain_logs
    from pycastle.run_lock import run_slot

    if improve_mode is not None and no_improve:
        click.echo(
            "Error: --improve and --no-improve are mutually exclusive.", err=True
        )
        sys.exit(1)

    _check_pycastle_dir_or_exit()
    _print_layer_summary()

    layout = resolve_layout()

    def _on_wait(message: str) -> None:
        click.echo(message, err=True)

    cfg: Config | None = None
    try:
        with run_slot(layout, ignore_global_lock=ignore_global_lock, on_wait=_on_wait):
            _refresh()
            cfg = _load_config_or_exit()
            _do_run(cfg, no_improve=no_improve, improve_mode_flag=improve_mode)
    except RunAlreadyInProgressError as exc:
        click.echo(
            f"Warning: a run is already in progress for project {exc.project!r}",
            err=True,
        )
        sys.exit(1)
    except RunSlotTimeoutError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if cfg is not None:
        maintain_logs(resolve_logs_dir(cfg), max_lines=10000, retention_days=30)


@main.command("cron", hidden=True)
@_NO_IMPROVE_OPTION
@click.pass_context
def cron_cmd(ctx: click.Context, *, no_improve: bool) -> None:
    click.echo(
        "Warning: 'pycastle cron' is deprecated — use 'pycastle run' instead.",
        err=True,
    )
    ctx.invoke(run_cmd, improve_mode=None, no_improve=no_improve)


if __name__ == "__main__":
    main()
