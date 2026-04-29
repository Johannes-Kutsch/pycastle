import asyncio
import contextlib
import dataclasses
import json
import re
import shutil
import subprocess
import sys
import traceback
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent_result import (
    AgentIncomplete,
    AgentSuccess,
    CancellationToken,
    IssueNumberParseFailure,
    PlanParseFailure,
    PreflightFailure,
    UsageLimitHit,
)
from .config import Config, StageOverride, config as _cfg
from .container_runner import run_agent as _default_run_agent
from .errors import PreflightError
from .git_service import GitCommandError, GitService
from .github_service import GithubService
from .validate import validate_config as _default_validate_config


@dataclasses.dataclass(frozen=True)
class IterationState:
    worktree_sha: str | None = None
    issues: list[dict] | None = None


@dataclasses.dataclass
class Deps:
    env: dict[str, str]
    repo_root: Path
    git_svc: GitService
    github_svc: GithubService
    run_agent: Any
    cfg: Config


@dataclasses.dataclass
class PlanResult:
    issues: list[dict]


@dataclasses.dataclass
class ImplementResult:
    completed: list[dict]
    errors: list[tuple[dict, Exception | PreflightFailure]]


@dataclasses.dataclass
class MergeResult:
    clean: list[dict]
    conflicts: list[dict]


async def wait_for_clean_working_tree(repo_root: Path, git_svc: GitService) -> None:
    if git_svc.is_working_tree_clean(repo_root):
        return
    print(
        "Working tree has uncommitted changes. "
        "Please commit or revert all local changes before the merge phase can proceed."
    )
    while not git_svc.is_working_tree_clean(repo_root):
        await asyncio.sleep(10)


def prune_orphan_worktrees(
    repo_root: Path, git_service: GitService | None = None
) -> None:
    worktrees_dir = repo_root / "pycastle" / ".worktrees"
    if not worktrees_dir.exists():
        return
    svc = git_service or GitService()
    active = {str(p) for p in svc.list_worktrees(repo_root)}
    for child in worktrees_dir.iterdir():
        if str(child.resolve()) not in active and child.is_dir():
            shutil.rmtree(child)


def delete_merged_branches(
    branches: list[str], repo_root: Path, git_service: GitService | None = None
) -> None:
    svc = git_service or GitService()
    for branch in branches:
        if not svc.is_ancestor(branch, repo_root):
            continue
        try:
            svc.delete_branch(branch, repo_root)
            print(f"Deleted merged branch: {branch}")
        except GitCommandError as e:
            print(f"Warning: could not delete branch {branch!r}: {e}", file=sys.stderr)


def _extract_text(output: str) -> str:
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            return obj.get("result", output)
    return output


MERGE_SANDBOX = "pycastle/merge-sandbox"


def branch_for(issue_number: int) -> str:
    return f"pycastle/issue-{issue_number}"


def strip_stale_blocker_refs(issues: list[dict]) -> list[dict]:
    open_numbers = {i["number"] for i in issues}
    result = []
    for issue in issues:
        body = issue.get("body") or ""
        lines = body.splitlines()
        cleaned = []
        for line in lines:
            if re.search(r"blocked\s+by\s+#\d+", line, re.IGNORECASE):
                refs = {int(m) for m in re.findall(r"#(\d+)", line)}
                if refs.isdisjoint(open_numbers):
                    continue
            cleaned.append(line)
        result.append({**issue, "body": "\n".join(cleaned)})
    return result


def parse_plan(output: str) -> list[dict] | PlanParseFailure:
    text = _extract_text(output)
    match = re.search(r"<plan>([\s\S]*?)</plan>", text)
    if not match:
        return PlanParseFailure(
            raw_output=text,
            detail="Planner produced no <plan> tag.",
        )
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        return PlanParseFailure(
            raw_output=text,
            detail=f"Planner produced malformed JSON inside <plan> tag: {exc}",
        )
    if "unblocked_issues" in data:
        raw = data["unblocked_issues"]
    elif "issues" in data:
        raw = data["issues"]
    else:
        return PlanParseFailure(
            raw_output=text,
            detail=f"Plan JSON has no 'unblocked_issues' or 'issues' key. Keys found: {list(data.keys())}",
        )
    return [{"number": i["number"], "title": i["title"]} for i in raw]


def _stage_for_agent(name: str) -> str:
    if name == "Planner":
        return "plan"
    if name.startswith("Implementer"):
        return "implement"
    if name.startswith("Reviewer"):
        return "review"
    if name == "Merger":
        return "merge"
    return ""


def _get_repo(repo_root: Path) -> str:
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        capture_output=True,
        cwd=repo_root,
    )
    if result.returncode != 0:
        raise RuntimeError("Could not determine GitHub repo name via gh")
    return result.stdout.decode("utf-8").strip()


def _format_feedback_commands(checks: Sequence[str]) -> str:
    wrapped = [f"`{cmd}`" for cmd in checks]
    if len(wrapped) <= 1:
        return "".join(wrapped)
    return ", ".join(wrapped[:-1]) + " and " + wrapped[-1]


async def _handle_preflight_failure(
    failures: list[tuple[str, str, str]],
    env: dict[str, str],
    repo_root: Path,
    github_svc: GithubService,
    run_agent: Any,
    hitl_label: str,
    prompts_dir: Path,
) -> tuple[str, int] | IssueNumberParseFailure:
    """Spawn preflight-issue agent for the first failing check; returns ('hitl'|'afk', issue_number)."""
    check_name, command, output = failures[0]
    agent_result = await run_agent(
        name=f"preflight-issue ({check_name})",
        prompt_file=prompts_dir / "preflight-issue.md",
        mount_path=repo_root,
        env=env,
        prompt_args={"CHECK_NAME": check_name, "COMMAND": command, "OUTPUT": output},
        skip_preflight=True,
    )
    if isinstance(agent_result, AgentSuccess):
        raw_text = agent_result.output
    elif isinstance(agent_result, AgentIncomplete):
        raw_text = agent_result.partial_output
    else:
        raw_text = str(agent_result)
    text = _extract_text(raw_text)
    match = re.search(r"<issue>(\d+)</issue>", text)
    if not match:
        return IssueNumberParseFailure(
            raw_output=text,
            detail="preflight-issue agent produced no <issue>NUMBER</issue> tag.",
        )
    issue_number = int(match.group(1))
    labels = github_svc.get_labels(issue_number)
    if hitl_label in labels:
        return "hitl", issue_number
    return "afk", issue_number


async def plan_phase(state: IterationState, deps: Deps) -> PlanResult:
    plan_result = await deps.run_agent(
        name="Planner",
        prompt_file=deps.cfg.prompts_dir / "plan-prompt.md",
        mount_path=deps.repo_root,
        env=deps.env,
        prompt_args={
            "OPEN_ISSUES_JSON": json.dumps(
                strip_stale_blocker_refs(
                    deps.github_svc.get_open_issues(deps.cfg.issue_label)
                )
            )
        },
        model=deps.cfg.plan_override.model,
        effort=deps.cfg.plan_override.effort,
        stage="pre-planning",
    )
    if isinstance(plan_result, AgentSuccess):
        plan_text = plan_result.output
    elif isinstance(plan_result, AgentIncomplete):
        plan_text = plan_result.partial_output
    else:
        plan_text = str(plan_result)
    parsed = parse_plan(plan_text)
    if isinstance(parsed, PlanParseFailure):
        raise RuntimeError(parsed.detail)
    return PlanResult(issues=parsed)


async def preflight_phase(deps: Deps) -> IterationState:
    sha = deps.git_svc.get_head_sha(deps.repo_root)
    state = IterationState(worktree_sha=sha)
    try:
        plan_result = await plan_phase(state, deps)
    except PreflightError as exc:
        preflight_result = await _handle_preflight_failure(
            exc.failures,
            deps.env,
            deps.repo_root,
            deps.github_svc,
            deps.run_agent,
            deps.cfg.hitl_label,
            deps.cfg.prompts_dir,
        )
        if isinstance(preflight_result, IssueNumberParseFailure):
            raise RuntimeError(preflight_result.detail) from None
        verdict, pf_num = preflight_result
        if verdict == "hitl":
            print(f"Preflight issue #{pf_num} requires human intervention. Exiting.")
            sys.exit(1)
        pf_title = deps.github_svc.get_issue_title(pf_num)
        return IterationState(
            worktree_sha=sha,
            issues=[{"number": pf_num, "title": pf_title}],
        )
    return IterationState(worktree_sha=sha, issues=plan_result.issues)


async def run_issue(
    issue: dict,
    env: dict[str, str],
    repo_root: Path,
    semaphore: asyncio.Semaphore | None = None,
    *,
    token: CancellationToken | None = None,
    cfg: Config | None = None,
    run_agent: Any | None = None,
    sha: str | None = None,
) -> dict | UsageLimitHit | PreflightFailure | None:
    cfg = cfg if cfg is not None else _cfg
    _run_agent = run_agent or _default_run_agent
    _branch = branch_for(issue["number"])
    prompt_args = {
        "ISSUE_NUMBER": str(issue["number"]),
        "ISSUE_TITLE": issue["title"],
        "BRANCH": _branch,
        "FEEDBACK_COMMANDS": _format_feedback_commands(cfg.implement_checks),
    }

    async def _bounded_run_agent(**kwargs: Any) -> Any:
        async with semaphore or contextlib.nullcontext():
            return await _run_agent(**kwargs, token=token)

    result = await _bounded_run_agent(
        name=f"Implementer #{issue['number']}",
        prompt_file=cfg.prompts_dir / "implement-prompt.md",
        mount_path=repo_root,
        env=env,
        prompt_args=prompt_args,
        branch=_branch,
        model=cfg.implement_override.model,
        effort=cfg.implement_override.effort,
        stage="pre-implementation",
        sha=sha,
        skip_preflight=True,
    )
    if isinstance(result, UsageLimitHit):
        return result
    if isinstance(result, PreflightFailure):
        return result
    if not isinstance(result, AgentSuccess):
        return None
    reviewer_prompt_args = {
        "ISSUE_NUMBER": str(issue["number"]),
        "ISSUE_TITLE": issue["title"],
        "BRANCH": _branch,
        "FEEDBACK_COMMANDS": _format_feedback_commands(cfg.implement_checks),
    }
    review_result = await _bounded_run_agent(
        name=f"Reviewer #{issue['number']}",
        prompt_file=cfg.prompts_dir / "review-prompt.md",
        mount_path=repo_root,
        env=env,
        prompt_args=reviewer_prompt_args,
        branch=_branch,
        model=cfg.review_override.model,
        effort=cfg.review_override.effort,
        stage="pre-review",
        skip_preflight=True,
    )
    if isinstance(review_result, UsageLimitHit):
        return review_result
    return issue


async def implement_phase(
    issues: list[dict],
    state: IterationState,
    deps: Deps,
    *,
    token: CancellationToken | None = None,
) -> ImplementResult:
    _token = token if token is not None else CancellationToken()
    semaphore = asyncio.Semaphore(deps.cfg.max_parallel)
    results = await asyncio.gather(
        *[
            run_issue(
                i,
                deps.env,
                deps.repo_root,
                semaphore,
                token=_token,
                cfg=deps.cfg,
                run_agent=deps.run_agent,
                sha=state.worktree_sha,
            )
            for i in issues
        ],
        return_exceptions=True,
    )
    if any(isinstance(r, UsageLimitHit) for r in results):
        print(
            "Usage limit reached. Worktrees preserved. Run 'pycastle run' again to resume.",
            file=sys.stderr,
        )
        sys.exit(1)
    completed: list[dict] = []
    errors: list[tuple[dict, Exception | PreflightFailure]] = []
    for issue, result in zip(issues, results):
        if isinstance(result, (Exception, PreflightFailure)):
            errors.append((issue, result))
        elif isinstance(result, dict):
            completed.append(issue)
    return ImplementResult(completed=completed, errors=errors)


async def merge_phase(completed: list[dict], deps: Deps) -> MergeResult:
    await wait_for_clean_working_tree(deps.repo_root, deps.git_svc)

    conflict_issues: list[dict] = []
    for issue in completed:
        if deps.git_svc.try_merge(deps.repo_root, branch_for(issue["number"])):
            deps.github_svc.close_issue(issue["number"])
        else:
            conflict_issues.append(issue)

    clean_issues = [i for i in completed if i not in conflict_issues]

    if clean_issues:
        deps.github_svc.close_completed_parent_issues()

    delete_merged_branches(
        [branch_for(i["number"]) for i in clean_issues], deps.repo_root, deps.git_svc
    )

    if conflict_issues:
        target_branch = deps.git_svc.get_current_branch(deps.repo_root)
        _sandbox_worktree = (
            deps.repo_root
            / deps.cfg.pycastle_dir
            / ".worktrees"
            / re.sub(r"[^a-z0-9]+", "-", MERGE_SANDBOX.lower()).strip("-")
        )
        merger_result: Any = None
        try:
            merger_result = await deps.run_agent(
                name="Merger",
                prompt_file=deps.cfg.prompts_dir / "merge-prompt.md",
                mount_path=deps.repo_root,
                env=deps.env,
                branch=MERGE_SANDBOX,
                prompt_args={
                    "BRANCHES": "\n".join(
                        f"- {branch_for(i['number'])}" for i in conflict_issues
                    ),
                    "CHECKS": " && ".join(cmd for _, cmd in deps.cfg.preflight_checks),
                },
                model=deps.cfg.merge_override.model,
                effort=deps.cfg.merge_override.effort,
                stage="pre-merge",
            )
        finally:
            try:
                deps.git_svc.remove_worktree(deps.repo_root, _sandbox_worktree)
            except Exception as exc:
                print(
                    f"Warning: could not remove sandbox worktree: {exc}",
                    file=sys.stderr,
                )
            try:
                deps.git_svc.delete_branch(MERGE_SANDBOX, deps.repo_root)
            except Exception as exc:
                print(
                    f"Warning: could not delete sandbox branch: {exc}", file=sys.stderr
                )
        if isinstance(merger_result, AgentSuccess):
            deps.git_svc.fast_forward_branch(
                deps.repo_root, target_branch, MERGE_SANDBOX
            )
        print("\nBranches merged.")
        delete_merged_branches(
            [branch_for(i["number"]) for i in conflict_issues],
            deps.repo_root,
            deps.git_svc,
        )
        for issue in conflict_issues:
            deps.github_svc.close_issue(issue["number"])
        deps.github_svc.close_completed_parent_issues()

    return MergeResult(clean=clean_issues, conflicts=conflict_issues)


async def run(
    env: dict[str, str],
    repo_root: Path,
    *,
    run_agent: Any | None = None,
    validate_config: Any | None = None,
    git_service: GitService | None = None,
    github_service: GithubService | None = None,
    cfg: Config | None = None,
) -> None:
    cfg = cfg if cfg is not None else _cfg
    _run_agent = run_agent or _default_run_agent
    _validate_config = validate_config or _default_validate_config

    _overrides = {
        "plan": {"model": cfg.plan_override.model, "effort": cfg.plan_override.effort},
        "implement": {
            "model": cfg.implement_override.model,
            "effort": cfg.implement_override.effort,
        },
        "review": {
            "model": cfg.review_override.model,
            "effort": cfg.review_override.effort,
        },
        "merge": {
            "model": cfg.merge_override.model,
            "effort": cfg.merge_override.effort,
        },
    }
    _validate_config(_overrides)
    cfg = dataclasses.replace(
        cfg,
        plan_override=StageOverride(
            model=_overrides["plan"]["model"], effort=_overrides["plan"]["effort"]
        ),
        implement_override=StageOverride(
            model=_overrides["implement"]["model"],
            effort=_overrides["implement"]["effort"],
        ),
        review_override=StageOverride(
            model=_overrides["review"]["model"], effort=_overrides["review"]["effort"]
        ),
        merge_override=StageOverride(
            model=_overrides["merge"]["model"], effort=_overrides["merge"]["effort"]
        ),
    )
    prune_orphan_worktrees(repo_root)
    git_svc = git_service or GitService()
    _lazy_github_svc: GithubService | None = None

    def _get_github_svc() -> GithubService:
        nonlocal _lazy_github_svc
        if _lazy_github_svc is None:
            _lazy_github_svc = github_service or GithubService(
                repo=_get_repo(repo_root)
            )
        return _lazy_github_svc

    for iteration in range(1, cfg.max_iterations + 1):
        print(f"\n=== Iteration {iteration}/{cfg.max_iterations} ===\n")

        if not _get_github_svc().has_open_issues_with_label(cfg.issue_label):
            print(f"No issues with label '{cfg.issue_label}' found. Skipping.")
            break

        deps = Deps(
            env=env,
            repo_root=repo_root,
            git_svc=git_svc,
            github_svc=_get_github_svc(),
            run_agent=_run_agent,
            cfg=cfg,
        )
        state = await preflight_phase(deps)
        issues: list[dict] = state.issues or []

        if not issues:
            print(f"No issues with label '{cfg.issue_label}' found. Skipping.")
            break

        print(f"Planning complete. {len(issues)} issue(s):")
        for issue in issues:
            print(
                f"  #{issue['number']}: {issue['title']} → {branch_for(issue['number'])}"
            )

        token = CancellationToken()
        impl_result = await implement_phase(issues, state, deps, token=token)

        for issue, error in impl_result.errors:
            match error:
                case PreflightFailure(failures=fs):
                    print(
                        f"  ✗ #{issue['number']} ({branch_for(issue['number'])}) pre-flight failed:"
                    )
                    for check_name, command, output in fs:
                        print(f"    ✗ {check_name} ({command}): {output}")
                case _:
                    tb = "".join(
                        traceback.format_exception(
                            type(error), error, error.__traceback__
                        )
                    )
                    timestamp = datetime.now(timezone.utc).isoformat()
                    entry = f"--- {timestamp} ---\n{tb}\n"
                    print(entry, file=sys.stderr)
                    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
                    with open(cfg.logs_dir / "errors.log", "a", encoding="utf-8") as f:
                        f.write(entry)
                    print(
                        f"  ✗ #{issue['number']} ({branch_for(issue['number'])}) failed: {error}"
                    )

        completed = impl_result.completed

        if not completed:
            print("No commits produced. Nothing to merge.")
            continue

        print(f"\nExecution complete. {len(completed)} branch(es) with commits:")
        for i in completed:
            print(f"  {branch_for(i['number'])}")

        await merge_phase(completed, deps)

    print("\nAll done.")
