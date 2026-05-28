#!/usr/bin/env python3
import asyncio
import difflib
import os
import sys
from pathlib import Path
from typing import Any, Literal

import click

from .config import Config, StageOverride, load_config, load_env, resolve_global_dir
from .config.loader import describe_config_layers
from .errors import (
    ClaudeCliNotFoundError,
    ConfigValidationError,
    DockerServiceError,
)
from .display.status_display import PlainStatusDisplay

_KNOWN_SERVICES: frozenset[str] = frozenset({"claude", "codex"})


def _stage_overrides(cfg: Config) -> list[tuple[str, StageOverride]]:
    return [
        ("plan", cfg.plan_override),
        ("implement", cfg.implement_override),
        ("review", cfg.review_override),
        ("merge", cfg.merge_override),
        ("preflight_issue", cfg.preflight_issue_override),
        ("improve", cfg.improve_override),
    ]


def _validate_stage_overrides(
    cfg: Config,
    valid_efforts_by_service: dict[str, frozenset[str]],
    valid_models_by_service: dict[str, frozenset[str]] | None = None,
) -> list[str]:
    if valid_models_by_service is None:
        valid_models_by_service = {}
    violations: list[str] = []
    for stage_name, override in _stage_overrides(cfg):
        node: StageOverride | None = override
        fallback_depth = 0
        while node is not None:
            stage_label = (
                stage_name if fallback_depth == 0 else f"{stage_name} fallback"
            )
            svc_name = node.service
            valid_efforts: frozenset[str] | None = None
            if not svc_name:
                violations.append(f"  stage={stage_label!r}: service is required")
            else:
                valid_efforts = valid_efforts_by_service.get(svc_name)
                if valid_efforts is None:
                    violations.append(
                        f"  stage={stage_label!r}: service={svc_name!r} is not a known service"
                        f" (known: {sorted(_KNOWN_SERVICES)})"
                    )
            if not node.effort:
                violations.append(f"  stage={stage_label!r}: effort is required")
            elif valid_efforts is not None and node.effort not in valid_efforts:
                violations.append(
                    f"  stage={stage_label!r}: effort={node.effort!r} is invalid"
                    f" for service={svc_name!r} (valid: {sorted(valid_efforts)})"
                )
            if svc_name and node.model:
                valid_models = valid_models_by_service.get(svc_name)
                if valid_models is not None and node.model not in valid_models:
                    suggestion = difflib.get_close_matches(
                        node.model, sorted(valid_models), n=1
                    )
                    detail = (
                        f' Did you mean "{suggestion[0]}"?'
                        if suggestion
                        else f" (valid: {sorted(valid_models)})"
                    )
                    violations.append(
                        f"  stage={stage_label!r}: model={node.model!r} is invalid"
                        f" for service={svc_name!r}.{detail}"
                    )
            node = node.fallback
            fallback_depth += 1
    return violations


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
    help="Re-pull bundled project-shaped files (Dockerfile, .gitignore, prompts/**) "
    "into ./pycastle/ without prompts. Leaves config.py and .env untouched.",
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
        _build(no_cache, cfg=cfg)
    except (ConfigValidationError, DockerServiceError) as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)


def _do_run(
    cfg: Config,
    no_improve: bool,
    improve_mode_flag: str | None,
) -> None:
    from typing import cast

    from .commands.build import main as _build
    from .config.loader import referenced_services
    from .iteration._deps import ImproveMode
    from .iteration.orchestrator import run
    from .services.agent_service import AgentService
    from .services.claude_service import ClaudeService
    from .services.codex_service import CodexService
    from .services.service_registry import ServiceRegistry

    validation_services: dict[str, AgentService] = {
        "claude": ClaudeService(),
        "codex": CodexService(),
    }
    valid_efforts_by_service = {
        name: svc.valid_efforts() for name, svc in validation_services.items()
    }
    valid_models_by_service = {
        name: svc.valid_models() for name, svc in validation_services.items()
    }
    violations = _validate_stage_overrides(
        cfg, valid_efforts_by_service, valid_models_by_service
    )
    if violations:
        click.echo(
            "Config validation errors:\n" + "\n".join(violations),
            err=True,
        )
        sys.exit(1)

    env = _load_env(cfg=cfg)
    referenced = referenced_services(cfg)
    if "both" in referenced:
        referenced = (referenced - {"both"}) | {"claude", "codex"}
    service_registry: dict[str, AgentService] = {}
    if "claude" in referenced:
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
        service_registry["claude"] = ClaudeService(accounts=accounts)
    if "codex" in referenced:
        service_registry["codex"] = CodexService()

    try:
        _build(stream=True, terse=True, cfg=cfg)
    except (ConfigValidationError, DockerServiceError) as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    # Strip the secondary token from container env; ClaudeService picks the
    # active token from its internal pool per session.
    container_env = {
        k: v for k, v in env.items() if k != "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY"
    }
    if no_improve:
        effective_improve_mode = None
    elif improve_mode_flag is not None:
        effective_improve_mode = improve_mode_flag
    else:
        effective_improve_mode = cfg.improve_mode
    registry = ServiceRegistry(service_registry)
    asyncio.run(
        run(
            container_env,
            Path(".").resolve(),
            service_registry=registry,
            improve_mode=cast(ImproveMode, effective_improve_mode),
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

    cfg = _load_config_or_exit()
    home = resolve_global_dir(None, os.environ)
    lock_path = home / ".cron.lock"
    home.mkdir(parents=True, exist_ok=True)

    _LOCK_TIMEOUT_SECS = 6 * 3600

    with open(lock_path, "w") as lock_file:
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
        _do_run(cfg, no_improve=no_improve, improve_mode_flag=None)

    logs_dir = (
        cfg.logs_dir
        if cfg.logs_dir.is_absolute()
        else (Path.cwd() / cfg.logs_dir).resolve()
    )
    maintain_logs(logs_dir, max_lines=10000, retention_days=30)


if __name__ == "__main__":
    main()
