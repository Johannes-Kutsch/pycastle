import dataclasses
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from ..agents.runner import AgentRunner, AgentRunnerProtocol
from ..config import load_config
from . import (
    AbortedAgentFailure,
    AbortedHardApiError,
    AbortedHITL,
    AbortedOperatorActionable,
    AbortedTimeout,
    AbortedUsageLimit,
    Continue,
    Done,
    NoCandidate,
    run_iteration,
)
from ..bug_reporter import file_operator_actionable_git_issue
from ._deps import Deps as IterationDeps, ImproveMode
from .preflight import PreflightCache
from ..display.rich_status_display import RichStatusDisplay
from ..services import (
    GitCommandError,
    GithubAuthError,
    GithubService,
    GitService,
    ServiceRegistry,
)
from ..services._wake_time import compute_wake_time
from ..session import SESSION_DIR_NAME
from ..display.status_display import StatusDisplay
from ..infrastructure.worktree import prune_orphan_worktrees
from ..log_maintenance import maintain_logs
from .. import _time as _time_module


class FileLogger:
    def __init__(self, logs_dir: Path) -> None:
        self._logs_dir = logs_dir

    def log_error(self, issue: dict, error: Exception) -> None:
        tb = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        timestamp = _time_module.now_local().isoformat()
        entry = f"--- {timestamp} ---\n{tb}\n"
        print(entry, file=sys.stderr)
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        with open(self._logs_dir / "errors.log", "a", encoding="utf-8") as f:
            f.write(entry)

    def log_agent_output(self, agent_name: str, output: str) -> None:
        pass


_SESSION_EXCLUDES = (f"{SESSION_DIR_NAME}/", ".claude/")


def _fmt_wake(wake: datetime, now: datetime) -> str:
    if wake.date() != now.date():
        return f"{wake:%b} {wake.day}, {wake:%H:%M}"
    return wake.strftime("%H:%M")


def ensure_session_excludes(repo_root: Path) -> None:
    exclude_file = repo_root / ".git" / "info" / "exclude"
    if not exclude_file.parent.exists():
        return
    existing = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
    additions = [e for e in _SESSION_EXCLUDES if e not in existing]
    if additions:
        with open(exclude_file, "a", encoding="utf-8") as f:
            for entry in additions:
                f.write(f"{entry}\n")


async def run(
    env: dict[str, str],
    repo_root: Path,
    *,
    agent_runner: AgentRunnerProtocol | None = None,
    git_service: GitService | None = None,
    github_service: GithubService | None = None,
    status_display: StatusDisplay | None = None,
    service_registry: ServiceRegistry | None = None,
    improve_mode: ImproveMode = None,
) -> None:
    cfg = load_config(repo_root=repo_root)
    prune_orphan_worktrees(repo_root, cfg=cfg)
    ensure_session_excludes(repo_root)
    git_svc = git_service or GitService(cfg)

    _owned_display: RichStatusDisplay | None = None
    if status_display is None:
        _owned_display = RichStatusDisplay()
        status_display = _owned_display  # type: ignore[assignment]

    try:
        git_svc.get_user_name(cwd=repo_root)
        git_svc.get_user_email(cwd=repo_root)
    except GitCommandError:
        print(
            "Git user not configured. Run:\n"
            "git config --global user.name 'Your Name' && "
            "git config --global user.email 'you@example.com'",
            file=sys.stderr,
        )
        sys.exit(1)

    if github_service is None:
        token = env.get("GH_TOKEN", "").strip()
        if not token:
            print(
                "GH_TOKEN is not set. Add it to pycastle/.env or your environment.",
                file=sys.stderr,
            )
            sys.exit(1)
        remote = git_svc.get_github_remote_repo(cwd=repo_root)
        if remote is None:
            print(
                "Could not determine GitHub repo from origin remote.",
                file=sys.stderr,
            )
            sys.exit(1)
        owner, repo = remote
        github_service = GithubService(f"{owner}/{repo}", token, cfg)

    try:
        login = github_service.check_auth()
    except GithubAuthError as exc:
        print(
            f"GitHub authentication failed: {exc.body}",
            file=sys.stderr,
        )
        sys.exit(1)

    status_display.print("", f"Authenticated as @{login}")  # type: ignore[union-attr]

    if service_registry:
        for line in service_registry.summary_lines():
            status_display.print("", line)  # type: ignore[union-attr]

    slept_once = False
    improve_dispatched_count = 0
    preflight_cache = PreflightCache()

    try:
        for iteration in range(1, cfg.max_iterations + 1):
            status_display.print(  # type: ignore[union-attr]
                "",
                f"=== Iteration {iteration}/{cfg.max_iterations} ===",
            )

            _now = _time_module.now_local()
            _iter_cfg = (
                dataclasses.replace(
                    cfg,
                    plan_override=service_registry.resolve(cfg.plan_override, _now),
                    implement_override=service_registry.resolve(
                        cfg.implement_override, _now
                    ),
                    review_override=service_registry.resolve(cfg.review_override, _now),
                    merge_override=service_registry.resolve(cfg.merge_override, _now),
                    preflight_issue_override=service_registry.resolve(
                        cfg.preflight_issue_override, _now
                    ),
                    improve_override=service_registry.resolve(
                        cfg.improve_override, _now
                    ),
                )
                if service_registry
                else cfg
            )

            if agent_runner is not None:
                _agent_runner: AgentRunnerProtocol = agent_runner
            else:
                _svc_name = _iter_cfg.default_service
                _svc = service_registry[_svc_name] if service_registry else None
                _agent_runner = AgentRunner(
                    env=env,
                    cfg=_iter_cfg,
                    git_service=git_svc,
                    service=_svc,
                )

            deps = IterationDeps(
                repo_root=repo_root,
                git_svc=git_svc,
                github_svc=github_service,
                agent_runner=_agent_runner,
                cfg=_iter_cfg,
                logger=FileLogger(_iter_cfg.logs_dir),
                status_display=status_display,  # type: ignore[arg-type]
                improve_mode=improve_mode,
                slept_once=slept_once,
                improve_dispatched_count=improve_dispatched_count,
                preflight_cache=preflight_cache,
            )
            outcome = await run_iteration(deps)
            improve_dispatched_count = deps.improve_dispatched_count

            match outcome:
                case Done(improve_cap_reached=True):
                    status_display.print(  # type: ignore[union-attr]
                        "",
                        f"improve_max ({cfg.improve_max}) dispatches reached. Stopping.",
                    )
                    break
                case Done():
                    status_display.print(  # type: ignore[union-attr]
                        "",
                        f"No issues with label '{cfg.issue_label}' found. Skipping.",
                    )
                    break
                case NoCandidate():
                    status_display.print(  # type: ignore[union-attr]
                        "",
                        "Improve agent reported no improvement candidate.",
                    )
                    break
                case AbortedHITL():
                    sys.exit(1)
                case AbortedHardApiError():
                    sys.exit(1)
                case AbortedUsageLimit(reset_time=reset_time):
                    now = _time_module.now_local()
                    if service_registry is not None and service_registry.has_available(
                        now
                    ):
                        exhausted_wake = service_registry.next_wake_time(now)
                        if exhausted_wake is not None:
                            status_display.print(  # type: ignore[union-attr]
                                "",
                                f"Account exhausted until {_fmt_wake(exhausted_wake, now)}, "
                                "switching to next available.",
                            )
                        continue
                    next_wake = (
                        service_registry.next_wake_time(now)
                        if service_registry is not None
                        else None
                    )
                    if next_wake is not None:
                        wake_time = next_wake
                        suffix = ""
                    else:
                        wake_time, is_estimated = compute_wake_time(reset_time, now)
                        suffix = " (estimated)" if is_estimated else ""
                    status_display.print(  # type: ignore[union-attr]
                        "",
                        f"Usage limit reached. Sleeping until {_fmt_wake(wake_time, now)}{suffix}."
                        " Press Ctrl+C to abort.",
                    )
                    time.sleep((wake_time - now).total_seconds())
                    slept_once = True
                    continue
                case AbortedAgentFailure(failed_role=role, issue_number=issue_num):
                    msg = f"Agent '{role}' failed irrecoverably."
                    if issue_num is not None:
                        msg += f" Filed issue #{issue_num} for triage."
                    status_display.print("", msg)  # type: ignore[union-attr]
                    sys.exit(1)
                case AbortedTimeout(failed_role=role):
                    status_display.print(  # type: ignore[union-attr]
                        "",
                        f"Agent '{role}' timed out. Resuming next iteration.",
                    )
                    continue
                case AbortedOperatorActionable(op=op, stderr=stderr, attempt_count=cnt):
                    status_display.print(  # type: ignore[union-attr]
                        "",
                        f"git {op} failed after {cnt} attempt(s) — remote unreachable. "
                        "Check SSH/network and retry.",
                    )
                    file_operator_actionable_git_issue(
                        op=op,
                        stderr=stderr,
                        attempt_count=cnt,
                        github_svc=github_service,
                    )
                    sys.exit(1)
                case Continue():
                    pass

        status_display.print("", "All done.")  # type: ignore[union-attr]
    finally:
        maintain_logs(cfg.logs_dir, 10_000, 30)
        if _owned_display is not None:
            _owned_display.stop()
