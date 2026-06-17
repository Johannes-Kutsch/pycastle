#!/usr/bin/env python3
import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import click
import pycastle_agent_runtime as runtime_package

from .config import (
    Config,
    load_config,
    load_credential_env,
    resolve_logs_dir,
)
from .errors import (
    ClaudeCliNotFoundError,
    ConfigValidationError,
    DockerServiceError,
)
from .layout import describe_config_layers, resolve_layout
from . import orchestration as pycastle_orchestration
from .run_startup_preparation import (
    RunStartupImproveModeFlagFacts,
    configured_provider_adapters_for_run,
    prepare_run_startup,
)
from ._universal_image_build import UniversalImageBuildOptions
from .display.status_display import PlainStatusDisplay

if TYPE_CHECKING:
    from .services.agent_service import AgentService


class _AgentRuntimeAdapter:
    def __init__(self) -> None:
        self.ServiceRegistry = runtime_package.ServiceRegistry
        self.chain_entries = runtime_package.chain_entries
        self.render_chain_label = runtime_package.render_chain_label
        self.validation_labels = runtime_package.validation_labels

    def __getattr__(self, name: str) -> Any:
        if name == "run":
            return pycastle_orchestration.run
        raise AttributeError(name)


agent_runtime: Any = _AgentRuntimeAdapter()


def _load_env(cfg: Config | None = None) -> dict[str, str]:
    if cfg is None:
        load_config()
    return load_credential_env()


def _configured_service_registry(
    cfg: Config, env: dict[str, str]
) -> dict[str, "AgentService"]:
    return configured_provider_adapters_for_run(cfg, env)


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
    from .infrastructure.shutdown_hook import install_urllib3_shutdown_hook

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
def init_cmd(global_flag: bool, local_flag: bool, refresh_flag: bool) -> None:
    from .commands.init import main as _init
    from .commands.init import refresh as _refresh

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
    from .commands.labels import main as _labels

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
    from .commands.build import main as _build

    _print_layer_summary()
    cfg = _load_config_or_exit()
    try:
        _build(options=UniversalImageBuildOptions(no_cache=no_cache), cfg=cfg)
    except (ConfigValidationError, DockerServiceError) as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)


@main.command("check")
def check_cmd() -> None:
    from .commands.check import main as _check

    _print_layer_summary()
    cfg = _load_config_or_exit()
    try:
        _check(cfg=cfg)
    except RuntimeError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)


def _do_run(
    cfg: Config,
    no_improve: bool,
    improve_mode_flag: str | None,
) -> None:
    from typing import cast

    from .commands.build import main as _build
    from .run_startup_preparation import RunImproveMode

    startup = prepare_run_startup(
        cfg,
        _load_env(cfg=cfg),
        RunStartupImproveModeFlagFacts(
            no_improve=no_improve,
            improve_mode_flag=cast(RunImproveMode, improve_mode_flag),
        ),
    )
    if startup.validation_failures:
        click.echo(
            "Config validation errors:\n"
            + "\n".join(failure.render() for failure in startup.validation_failures),
            err=True,
        )
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
            Path(".").resolve(),
            service_registry=startup.runtime_registry,
            improve_mode=startup.effective_improve_mode,
        )
    )


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
@click.option(
    "--no-improve",
    "no_improve",
    is_flag=True,
    default=False,
    help="Disable improve-agent dispatch for this run, overriding any improve_mode in config.",
)
def run_cmd(improve_mode: str | None, no_improve: bool) -> None:
    if improve_mode is not None and no_improve:
        click.echo(
            "Error: --improve and --no-improve are mutually exclusive.", err=True
        )
        sys.exit(1)

    _print_layer_summary()
    cfg = _load_config_or_exit()
    _do_run(cfg, no_improve=no_improve, improve_mode_flag=improve_mode)


@main.command("cron")
@click.option(
    "--no-improve",
    "no_improve",
    is_flag=True,
    default=False,
    help="Disable improve-agent dispatch for this run, overriding any improve_mode in config.",
)
def cron_cmd(no_improve: bool) -> None:
    import threading
    import time as _time

    from .commands.init import refresh as _refresh
    from .log_maintenance import maintain_logs

    layout = resolve_layout()
    layout.cron_lock_path.parent.mkdir(parents=True, exist_ok=True)

    _LOCK_TIMEOUT_SECS = 6 * 3600

    with open(layout.cron_lock_path, "w") as lock_file:
        if sys.platform == "win32":
            import msvcrt

            deadline = _time.monotonic() + _LOCK_TIMEOUT_SECS
            while True:
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if _time.monotonic() >= deadline:
                        click.echo(
                            "Error: timed out waiting for cron lock after 6 hours",
                            err=True,
                        )
                        sys.exit(1)
                    _time.sleep(1)
        else:
            import fcntl
            import signal

            _in_main_thread = threading.current_thread() is threading.main_thread()

            def _on_alarm(signum: int, frame: object) -> None:
                raise TimeoutError()

            if _in_main_thread:
                old_handler = signal.signal(signal.SIGALRM, _on_alarm)
                signal.alarm(_LOCK_TIMEOUT_SECS)
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                if _in_main_thread:
                    signal.alarm(0)
            except TimeoutError:
                click.echo(
                    "Error: timed out waiting for cron lock after 6 hours",
                    err=True,
                )
                sys.exit(1)
            finally:
                if _in_main_thread:
                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, old_handler)

        _print_layer_summary()
        _refresh()
        cfg = _load_config_or_exit()
        _do_run(cfg, no_improve=no_improve, improve_mode_flag=None)

    maintain_logs(resolve_logs_dir(cfg), max_lines=10000, retention_days=30)


if __name__ == "__main__":
    main()
