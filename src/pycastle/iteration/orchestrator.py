import dataclasses
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import cast

import click

from pycastle import _time as _time_module
from pycastle.agents.runner import AgentRunner, AgentRunnerProtocol
from pycastle.config import (
    Config,
    load_config,
    replace_config_runtime_fields,
    resolve_logs_dir,
)
from pycastle.display.rich_status_display import RichStatusDisplay
from pycastle.display.status_display import StatusDisplay
from pycastle.infrastructure.worktree import prune_orphan_worktrees
from pycastle.iteration import IterationOutcome, run_iteration
from pycastle.iteration._deps import Deps as IterationDeps
from pycastle.iteration._deps import ImproveMode
from pycastle.iteration._service_summary import render_service_summary_line
from pycastle.iteration.branch_resolution import (
    BranchFacts,
    BranchSetupPlan,
    Checkout,
    DevBranchMissing,
    Fetch,
    PushUpstream,
    Seed,
    UncleanWorkingTree,
    resolve_branch_setup,
)
from pycastle.iteration.outcome_routing import (
    BreakLoop,
    ContinueLoop,
    ExitFailure,
    RouterDeps,
    SleepThenContinue,
    route_outcome,
)
from pycastle.iteration.preflight import PreflightCache
from pycastle.log_maintenance import maintain_logs
from pycastle.services import (
    AgentService,
    GitCommandError,
    GithubAPIError,
    GithubAuthError,
    GithubService,
    GitService,
    OperatorActionableGithubError,
    ServiceRegistry,
)
from pycastle.session import SESSION_DIR_NAME


class FileLogger:
    def __init__(self, logs_dir: Path) -> None:
        self._logs_dir = logs_dir

    def log_error(self, _issue: dict, error: Exception) -> None:
        tb = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        timestamp = _time_module.now_local().isoformat()
        entry = f"--- {timestamp} ---\n{tb}\n"
        sys.stderr.write(entry)
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        with (self._logs_dir / "errors.log").open("a", encoding="utf-8") as f:
            f.write(entry)

    def log_internal_error(
        self, label: str, error: Exception, cause: Exception | None = None
    ) -> None:
        parts: list[str] = []
        timestamp = _time_module.now_local().isoformat()
        parts.append(f"--- {timestamp} ---")
        parts.append(label)
        if cause is not None:
            parts.append("--- Original failure ---")
            parts.append(
                "".join(
                    traceback.format_exception(type(cause), cause, cause.__traceback__)
                ).rstrip()
            )
        parts.append("--- Crash traceback ---")
        parts.append(
            "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            ).rstrip()
        )
        parts.append("")
        entry = "\n".join(parts) + "\n"
        sys.stderr.write(entry)
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        with (self._logs_dir / "errors.log").open("a", encoding="utf-8") as f:
            f.write(entry)

    def log_agent_output(self, agent_name: str, output: str) -> None:
        pass


_SESSION_EXCLUDES = (f"{SESSION_DIR_NAME}/", ".claude/")


@dataclasses.dataclass
class RunOptions:
    agent_runner: AgentRunnerProtocol | None = None
    git_service: GitService | None = None
    github_service: GithubService | None = None
    status_display: StatusDisplay | None = None
    service_registry: ServiceRegistry | None = None
    improve_mode: ImproveMode = None


def _github_retry_exhaustion_message(exc: OperatorActionableGithubError) -> str:
    return (
        "GitHub request retry limit reached: "
        f"{exc.method} {exc.path} failed after {exc.attempt_count} attempts. "
        "Check GitHub availability or network connectivity and retry."
    )


def ensure_session_excludes(repo_root: Path) -> None:
    exclude_file = repo_root / ".git" / "info" / "exclude"
    if not exclude_file.parent.exists():
        return
    existing = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
    additions = [e for e in _SESSION_EXCLUDES if e not in existing]
    if additions:
        with exclude_file.open("a", encoding="utf-8") as f:
            f.writelines(f"{entry}\n" for entry in additions)


def _init_display(
    status_display: StatusDisplay | None,
) -> tuple[RichStatusDisplay | None, StatusDisplay]:
    if status_display is None:
        owned = RichStatusDisplay()
        return owned, owned  # type: ignore[return-value]
    return None, status_display


def _create_github_service(
    env: dict[str, str],
    repo_root: Path,
    cfg: Config,
    git_svc: GitService,
) -> GithubService:
    token = env.get("GH_TOKEN", "").strip()
    if not token:
        raise click.UsageError(
            "GH_TOKEN is not set. Add it to pycastle/.env or your environment."
        )
    remote = git_svc.get_github_remote_repo(cwd=repo_root)
    if remote is None:
        raise click.UsageError("Could not determine GitHub repo from origin remote.")
    owner, repo = remote
    return GithubService(f"{owner}/{repo}", token, cfg)


def _resolve_github_service(
    env: dict[str, str],
    repo_root: Path,
    cfg: Config,
    git_svc: GitService,
    github_service: GithubService | None,
) -> GithubService:
    if github_service is not None:
        return github_service
    return _create_github_service(env, repo_root, cfg, git_svc)


def _collect_branch_facts(
    git_svc: GitService, repo_root: Path, cfg: Config
) -> BranchFacts:
    dev_branch_on_origin = git_svc.verify_ref_exists(
        f"refs/remotes/origin/{cfg.dev_branch}", repo_root
    )
    if cfg.working_branch is None:
        return BranchFacts(
            dev_branch_on_origin=dev_branch_on_origin,
            working_branch_on_local=False,
            working_branch_on_origin=False,
            working_tree_clean=git_svc.is_working_tree_clean(repo_root),
        )
    working_branch_on_local = git_svc.verify_ref_exists(cfg.working_branch, repo_root)
    working_branch_on_origin = git_svc.verify_ref_exists(
        f"refs/remotes/origin/{cfg.working_branch}", repo_root
    )
    return BranchFacts(
        dev_branch_on_origin=dev_branch_on_origin,
        working_branch_on_local=working_branch_on_local,
        working_branch_on_origin=working_branch_on_origin,
        working_tree_clean=git_svc.is_working_tree_clean(repo_root),
    )


def _apply_branch_setup_plan(
    git_svc: GitService, repo_root: Path, plan: BranchSetupPlan
) -> None:
    for step in plan.steps:
        if isinstance(step, Fetch):
            git_svc.fetch(repo_root)
        elif isinstance(step, Seed):
            git_svc.create_branch_from(repo_root, step.target, step.source)
        elif isinstance(step, Checkout):
            git_svc.checkout_branch(repo_root, step.branch)
        elif isinstance(step, PushUpstream):
            git_svc.push_upstream(repo_root, step.branch)


def _setup_branch(git_svc: GitService, repo_root: Path, cfg: Config) -> None:
    facts = _collect_branch_facts(git_svc, repo_root, cfg)
    result = resolve_branch_setup(cfg, facts)
    if isinstance(result, DevBranchMissing):
        raise click.UsageError(result.message)
    if isinstance(result, UncleanWorkingTree):
        raise click.UsageError(
            "Working tree is not clean. Commit or stash changes before running."
        )
    _apply_branch_setup_plan(git_svc, repo_root, result)


def _check_github_auth(github_service: GithubService) -> str:
    try:
        return github_service.check_auth()
    except GithubAuthError as exc:
        raise click.UsageError(f"GitHub authentication failed: {exc.body}") from exc
    except OperatorActionableGithubError as exc:
        raise click.UsageError(_github_retry_exhaustion_message(exc)) from exc


def _print_service_registry_summary(
    service_registry: ServiceRegistry | None,
    status_display: StatusDisplay,
) -> None:
    if service_registry:
        for line in service_registry.summary_lines(render_service_summary_line):
            status_display.print("", line)  # type: ignore[union-attr]


def _resolve_iter_cfg(
    cfg: Config,
    service_registry: ServiceRegistry | None,
    now: datetime,
) -> Config:
    if service_registry is None:
        return cfg
    return replace_config_runtime_fields(
        cfg,
        dataclasses.replace(
            cfg,
            plan_override=service_registry.resolve(cfg.plan_override, now),
            implement_override=service_registry.resolve(cfg.implement_override, now),
            review_override=service_registry.resolve(cfg.review_override, now),
            merge_override=service_registry.resolve(cfg.merge_override, now),
            improve_override=service_registry.resolve(cfg.improve_override, now),
        ),
    )


def _build_agent_runner(
    fixed_runner: AgentRunnerProtocol | None,
    env: dict[str, str],
    cfg: Config,
    git_svc: GitService,
    service_registry: ServiceRegistry | None,
) -> AgentRunnerProtocol:
    if fixed_runner is not None:
        return fixed_runner
    return AgentRunner(
        env=env,
        cfg=cfg,
        git_service=git_svc,
        service_registry=(
            cast("dict[str, AgentService]", service_registry.services)
            if service_registry is not None
            else None
        ),
    )


async def _run_one_iteration(
    deps: IterationDeps, status_display: StatusDisplay
) -> IterationOutcome:
    try:
        return await run_iteration(deps)
    except GithubAPIError as exc:
        status_display.print("", f"GitHub repository access failed: {exc}")  # type: ignore[union-attr]
        sys.exit(1)
    except OperatorActionableGithubError as exc:
        status_display.print("", _github_retry_exhaustion_message(exc))  # type: ignore[union-attr]
        sys.exit(1)


def _stop_display(owned_display: RichStatusDisplay | None) -> None:
    if owned_display is not None:
        owned_display.stop()


async def run(
    env: dict[str, str],
    repo_root: Path,
    opts: RunOptions | None = None,
) -> None:
    _opts = opts or RunOptions()
    cfg = load_config(repo_root=repo_root)
    prune_orphan_worktrees(repo_root, cfg=cfg)
    ensure_session_excludes(repo_root)
    git_svc = _opts.git_service or GitService(cfg)

    try:
        git_svc.get_user_name(cwd=repo_root)
        git_svc.get_user_email(cwd=repo_root)
    except GitCommandError as exc:
        raise click.UsageError(
            "Git user not configured. Run:\n"
            "git config --global user.name 'Your Name' && "
            "git config --global user.email 'you@example.com'"
        ) from exc

    github_service = _resolve_github_service(
        env, repo_root, cfg, git_svc, _opts.github_service
    )
    _owned_display, status_display = _init_display(_opts.status_display)
    login = _check_github_auth(github_service)
    status_display.print("", f"GitHub auth: authenticated as @{login}")  # type: ignore[union-attr]

    _setup_branch(git_svc, repo_root, cfg)

    service_registry = _opts.service_registry
    _print_service_registry_summary(service_registry, status_display)  # type: ignore[arg-type]

    slept_once = False
    improve_dispatched_count = 0
    improve_cycle_interrupted = False
    preflight_cache = PreflightCache()

    try:
        for iteration in range(1, cfg.max_iterations + 1):
            status_display.print(  # type: ignore[union-attr]
                "",
                f"=== Iteration {iteration}/{cfg.max_iterations} ===",
            )
            _now = _time_module.now_local()
            _iter_cfg = _resolve_iter_cfg(cfg, service_registry, _now)
            _agent_runner = _build_agent_runner(
                _opts.agent_runner, env, _iter_cfg, git_svc, service_registry
            )
            deps = IterationDeps(
                repo_root=repo_root,
                git_svc=git_svc,
                github_svc=github_service,
                agent_runner=_agent_runner,
                cfg=_iter_cfg,
                logger=FileLogger(resolve_logs_dir(_iter_cfg)),
                status_display=status_display,  # type: ignore[arg-type]
                service_registry=service_registry,
                improve_mode=_opts.improve_mode,
                slept_once=slept_once,
                improve_dispatched_count=improve_dispatched_count,
                improve_cycle_interrupted=improve_cycle_interrupted,
                preflight_cache=preflight_cache,
            )
            outcome = await _run_one_iteration(deps, status_display)  # type: ignore[arg-type]
            improve_dispatched_count = deps.improve_dispatched_count
            improve_cycle_interrupted = deps.improve_cycle_interrupted

            _post_iteration_now = _time_module.now_local()
            router_deps = RouterDeps(
                cfg=cfg,
                service_registry=service_registry,
                now=_post_iteration_now,
                status_display=status_display,  # type: ignore[arg-type]
                github_svc=github_service,
            )
            directive = route_outcome(outcome, router_deps)
            match directive:
                case ContinueLoop():
                    continue
                case SleepThenContinue(
                    wake_time=wake_time,
                    message=sleep_msg,
                    slept_once_after=slept_after,
                ):
                    status_display.print("", sleep_msg)  # type: ignore[union-attr]
                    time.sleep(  # noqa: ASYNC251  # intentional blocking sleep: caller runs this in asyncio.to_thread
                        max(0.0, (wake_time - _time_module.now_local()).total_seconds())
                    )
                    slept_once = slept_after
                    continue
                case BreakLoop():
                    break
                case ExitFailure(code=exit_code):
                    sys.exit(exit_code)

        status_display.print("", "All done.")  # type: ignore[union-attr]
    finally:
        maintain_logs(resolve_logs_dir(cfg), 10_000, 30)
        _stop_display(_owned_display)
