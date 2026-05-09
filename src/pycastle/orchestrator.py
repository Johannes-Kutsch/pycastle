import shutil
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .account_pool import AccountPool
from .agent_result import PreflightFailure
from .agent_runner import AgentRunner, AgentRunnerProtocol, RunRequest
from .config import Config, load_config
from .iteration import (
    AbortedAgentFailure,
    AbortedHITL,
    AbortedUsageLimit,
    Continue,
    Done,
    NoCandidate,
    run_iteration,
)
from .iteration._deps import Deps as IterationDeps
from .iteration.dispatcher import ImproveMode
from .rich_status_display import RichStatusDisplay
from .services import (
    GitCommandError,
    GithubAuthError,
    GithubService,
    GitService,
)
from .status_display import StatusDisplay
from .worktree import remove_worktrees_dir_if_empty


class FileLogger:
    def __init__(self, logs_dir: Path) -> None:
        self._logs_dir = logs_dir

    def log_error(self, issue: dict, error: Exception | PreflightFailure) -> None:
        if isinstance(error, PreflightFailure):
            return
        tb = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        timestamp = datetime.now(timezone.utc).isoformat()
        entry = f"--- {timestamp} ---\n{tb}\n"
        print(entry, file=sys.stderr)
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        with open(self._logs_dir / "errors.log", "a", encoding="utf-8") as f:
            f.write(entry)

    def log_agent_output(self, agent_name: str, output: str) -> None:
        pass


_SESSION_EXCLUDES = (".pycastle-session/", ".claude/")


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


def prune_orphan_worktrees(
    repo_root: Path,
    git_service: GitService | None = None,
    cfg: Config | None = None,
) -> None:
    worktrees_dir = repo_root / "pycastle" / ".worktrees"
    if not worktrees_dir.exists():
        return
    svc = git_service or GitService(cfg or load_config())
    active = {str(p) for p in svc.list_worktrees(repo_root)}
    for child in worktrees_dir.iterdir():
        if str(child.resolve()) not in active and child.is_dir():
            shutil.rmtree(child)
    remove_worktrees_dir_if_empty(worktrees_dir)


class _CallableAgentRunner:
    """Wraps a plain async callable as an AgentRunnerProtocol."""

    def __init__(self, fn: Any) -> None:
        self._fn = fn

    async def run(self, request: RunRequest) -> Any:
        return await self._fn(request)

    async def run_preflight(self, **kwargs: Any) -> list[tuple[str, str, str]]:
        return []


async def run(
    env: dict[str, str],
    repo_root: Path,
    *,
    run_agent: Any | None = None,
    agent_runner: AgentRunnerProtocol | None = None,
    git_service: GitService | None = None,
    github_service: GithubService | None = None,
    status_display: StatusDisplay | None = None,
    account_pool: AccountPool | None = None,
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

    if account_pool is not None:
        names = account_pool.names()
        if len(names) == 1:
            summary = f"Claude accounts: {names[0]} (active)"
        else:
            parts = [f"{names[0]} (active)"] + [f"{n} (standby)" for n in names[1:]]
            summary = "Claude accounts: " + ", ".join(parts)
        status_display.print("", summary)  # type: ignore[union-attr]

    slept_once = False

    try:
        for iteration in range(1, cfg.max_iterations + 1):
            status_display.print(  # type: ignore[union-attr]
                "",
                f"=== Iteration {iteration}/{cfg.max_iterations} ===",
            )

            if agent_runner is not None:
                _agent_runner: AgentRunnerProtocol = agent_runner
            elif run_agent is not None:
                _agent_runner = _CallableAgentRunner(run_agent)
            else:
                _agent_runner = AgentRunner(
                    env=env,
                    cfg=cfg,
                    git_service=git_svc,
                    account_pool=account_pool,
                )

            deps = IterationDeps(
                repo_root=repo_root,
                git_svc=git_svc,
                github_svc=github_service,
                agent_runner=_agent_runner,
                cfg=cfg,
                logger=FileLogger(cfg.logs_dir),
                status_display=status_display,  # type: ignore[arg-type]
                improve_mode=improve_mode,
                slept_once=slept_once,
            )
            outcome = await run_iteration(deps)

            match outcome:
                case Done():
                    status_display.print(  # type: ignore[union-attr]
                        "",
                        f"No issues with label '{cfg.issue_label}' found. Skipping.",
                    )
                    break
                case NoCandidate():
                    status_display.print(  # type: ignore[union-attr]
                        "",
                        "No improvement candidate found.",
                    )
                    break
                case AbortedHITL():
                    sys.exit(1)
                case AbortedUsageLimit(reset_time=reset_time):
                    now = datetime.now()
                    if account_pool is not None and account_pool.has_available(now=now):
                        next_name, _ = account_pool.pick(now=now)
                        wake = account_pool.earliest_wake_time()
                        status_display.print(  # type: ignore[union-attr]
                            "",
                            f"Account exhausted until {wake.strftime('%H:%M')}, "
                            f"switching to '{next_name}'.",
                        )
                        continue
                    if account_pool is not None:
                        wake_time = account_pool.earliest_wake_time()
                        suffix = ""
                    elif reset_time is not None:
                        wake_time = reset_time + timedelta(minutes=2)
                        suffix = ""
                    else:
                        next_hour = now.replace(
                            minute=0, second=0, microsecond=0
                        ) + timedelta(hours=1)
                        wake_time = next_hour + timedelta(minutes=2)
                        suffix = " (estimated)"
                    status_display.print(  # type: ignore[union-attr]
                        "",
                        f"Usage limit reached. Sleeping until {wake_time.strftime('%H:%M')}{suffix}."
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
                case Continue():
                    pass

        status_display.print("", "All done.")  # type: ignore[union-attr]
    finally:
        if _owned_display is not None:
            _owned_display.stop()
