import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .agent_result import PreflightFailure
from .agent_runner import AgentRunner, AgentRunnerProtocol, RunRequest
from .config import Config, load_config
from .services import GitCommandError, GitService
from .services import GithubNotFoundError, GithubService
from .iteration import (
    AbortedHITL,
    AbortedUsageLimit,
    Continue,
    Done,
    run_iteration,
)
from .iteration._deps import Deps as IterationDeps
from .rich_status_display import RichStatusDisplay
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


def _get_repo(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            capture_output=True,
            cwd=repo_root,
        )
    except FileNotFoundError as exc:
        raise GithubNotFoundError("gh executable not found") from exc
    if result.returncode != 0:
        raise RuntimeError("Could not determine GitHub repo name via gh")
    return result.stdout.decode("utf-8").strip()


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
) -> None:
    cfg = load_config(repo_root=repo_root)
    prune_orphan_worktrees(repo_root, cfg=cfg)
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

    if github_service is None and shutil.which("gh") is None:
        print(
            "GitHub CLI not found. Install it with: sudo apt install gh,"
            " then run: gh auth login",
            file=sys.stderr,
        )
        sys.exit(1)

    _lazy_github_svc: GithubService | None = None

    def _get_github_svc() -> GithubService:
        nonlocal _lazy_github_svc
        if _lazy_github_svc is None:
            _lazy_github_svc = github_service or GithubService(
                repo=_get_repo(repo_root), cfg=cfg
            )
        return _lazy_github_svc

    try:
        for iteration in range(1, cfg.max_iterations + 1):
            status_display.print(  # type: ignore[union-attr]
                "",
                f"=== Iteration {iteration}/{cfg.max_iterations} ===",
            )

            if not _get_github_svc().has_open_issues_with_label(cfg.issue_label):
                status_display.print(  # type: ignore[union-attr]
                    "",
                    f"No issues with label '{cfg.issue_label}' found. Skipping.",
                )
                break

            if agent_runner is not None:
                _agent_runner: AgentRunnerProtocol = agent_runner
            elif run_agent is not None:
                _agent_runner = _CallableAgentRunner(run_agent)
            else:
                _agent_runner = AgentRunner(env=env, cfg=cfg, git_service=git_svc)

            deps = IterationDeps(
                repo_root=repo_root,
                git_svc=git_svc,
                github_svc=_get_github_svc(),
                agent_runner=_agent_runner,
                cfg=cfg,
                logger=FileLogger(cfg.logs_dir),
                status_display=status_display,  # type: ignore[arg-type]
            )
            outcome = await run_iteration(deps)

            match outcome:
                case Done():
                    status_display.print(  # type: ignore[union-attr]
                        "",
                        f"No issues with label '{cfg.issue_label}' found. Skipping.",
                    )
                    break
                case AbortedHITL():
                    sys.exit(1)
                case AbortedUsageLimit():
                    now = datetime.now()
                    next_hour = now.replace(
                        minute=0, second=0, microsecond=0
                    ) + timedelta(hours=1)
                    wake_time = next_hour + timedelta(minutes=2)
                    status_display.print(  # type: ignore[union-attr]
                        "",
                        f"Usage limit reached. Sleeping until {wake_time.strftime('%H:%M')}."
                        " Press Ctrl+C to abort.",
                    )
                    time.sleep((wake_time - now).total_seconds())
                    continue
                case Continue():
                    pass

        status_display.print("", "All done.")  # type: ignore[union-attr]
    finally:
        if _owned_display is not None:
            _owned_display.stop()
