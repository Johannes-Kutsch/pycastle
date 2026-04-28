import asyncio
import contextlib
import json
import re
import shutil
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import (
    IMPLEMENT_CHECKS,
    ISSUE_LABEL,
    LOGS_DIR,
    MAX_ITERATIONS,
    MAX_PARALLEL,
    PREFLIGHT_CHECKS,
    PROMPTS_DIR,
    STAGE_OVERRIDES,
)
from .container_runner import run_agent as _default_run_agent
from .errors import PreflightError
from .git_service import GitCommandError, GitService
from .github_service import GithubService
from .validate import validate_config as _default_validate_config


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


def parse_plan(output: str) -> list[dict]:
    text = _extract_text(output)
    match = re.search(r"<plan>([\s\S]*?)</plan>", text)
    if not match:
        raise RuntimeError("Planner produced no <plan> tag.\n\n" + text)
    data = json.loads(match.group(1))
    if "unblocked_issues" in data:
        return data["unblocked_issues"]
    if "issues" in data:
        return data["issues"]
    raise RuntimeError(
        f"Plan JSON has no 'unblocked_issues' or 'issues' key. Keys found: {list(data.keys())}"
    )


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


def _run_host_checks_impl(
    checks: list[tuple[str, str]],
) -> list[tuple[str, str, str]]:
    failures = []
    for check_name, command in checks:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            failures.append((check_name, command, result.stdout + result.stderr))
    return failures


def _format_feedback_commands(checks: list[str]) -> str:
    wrapped = [f"`{cmd}`" for cmd in checks]
    if len(wrapped) <= 1:
        return "".join(wrapped)
    return ", ".join(wrapped[:-1]) + " and " + wrapped[-1]


async def run_issue(
    issue: dict,
    env: dict[str, str],
    repo_root: Path,
    overrides: dict | None = None,
    semaphore: asyncio.Semaphore | None = None,
    *,
    run_agent: Any | None = None,
    sha: str | None = None,
) -> dict | None:
    _run_agent = run_agent or _default_run_agent
    overrides = overrides or {}
    impl_stage = overrides.get("implement", {})
    rev_stage = overrides.get("review", {})
    prompt_args = {
        "ISSUE_NUMBER": str(issue["number"]),
        "ISSUE_TITLE": issue["title"],
        "BRANCH": issue["branch"],
        "FEEDBACK_COMMANDS": _format_feedback_commands(IMPLEMENT_CHECKS),
    }

    async def _bounded_run_agent(**kwargs: Any) -> str:
        async with semaphore or contextlib.nullcontext():
            return await _run_agent(**kwargs)

    result = await _bounded_run_agent(
        name=f"Implementer #{issue['number']}",
        prompt_file=PROMPTS_DIR / "implement-prompt.md",
        mount_path=repo_root,
        env=env,
        prompt_args=prompt_args,
        branch=issue["branch"],
        model=impl_stage.get("model", ""),
        effort=impl_stage.get("effort", ""),
        stage="pre-implementation",
        sha=sha,
    )
    if "<promise>COMPLETE</promise>" not in _extract_text(result):
        return None
    reviewer_prompt_args = {
        "ISSUE_NUMBER": str(issue["number"]),
        "ISSUE_TITLE": issue["title"],
        "BRANCH": issue["branch"],
    }
    await _bounded_run_agent(
        name=f"Reviewer #{issue['number']}",
        prompt_file=PROMPTS_DIR / "review-prompt.md",
        mount_path=repo_root,
        env=env,
        prompt_args=reviewer_prompt_args,
        branch=issue["branch"],
        model=rev_stage.get("model", ""),
        effort=rev_stage.get("effort", ""),
        stage="pre-review",
    )
    return issue


async def run(
    env: dict[str, str],
    repo_root: Path,
    *,
    run_agent: Any | None = None,
    validate_config: Any | None = None,
    git_service: GitService | None = None,
    github_service: GithubService | None = None,
    run_host_checks: Any | None = None,
    stage_overrides: dict | None = None,
    max_parallel: int | None = None,
    max_iterations: int | None = None,
    logs_dir: Path | None = None,
) -> None:
    _run_agent = run_agent or _default_run_agent
    _validate_config = validate_config or _default_validate_config
    _stage_overrides = (
        stage_overrides if stage_overrides is not None else STAGE_OVERRIDES
    )
    _max_parallel = max_parallel if max_parallel is not None else MAX_PARALLEL
    _max_iterations = max_iterations if max_iterations is not None else MAX_ITERATIONS
    _logs_dir = logs_dir if logs_dir is not None else LOGS_DIR
    _run_host_checks = run_host_checks or _run_host_checks_impl

    _validate_config(_stage_overrides)
    prune_orphan_worktrees(repo_root)
    git_svc = git_service or GitService()
    _safe_sha: str | None = None
    _skip_preflight: bool = False
    for iteration in range(1, _max_iterations + 1):
        print(f"\n=== Iteration {iteration}/{_max_iterations} ===\n")

        _ran_preflight = not _skip_preflight
        plan_stage = _stage_overrides.get("plan", {})
        try:
            plan_output = await _run_agent(
                name="Planner",
                prompt_file=PROMPTS_DIR / "plan-prompt.md",
                mount_path=repo_root,
                env=env,
                prompt_args={"ISSUE_LABEL": ISSUE_LABEL},
                model=plan_stage.get("model", ""),
                effort=plan_stage.get("effort", ""),
                stage="pre-planning",
                skip_preflight=_skip_preflight,
            )
        except PreflightError as exc:
            print("[Planner] Pre-flight failed — aborting run:")
            for check_name, command, output in exc.failures:
                print(f"  ✗ {check_name} ({command}): {output}")
            return
        _skip_preflight = False
        issues = parse_plan(plan_output)

        if not issues:
            print(f"No issues with label '{ISSUE_LABEL}' found. Skipping.")
            break

        if _ran_preflight:
            _safe_sha = git_svc.get_head_sha(repo_root)

        print(f"Planning complete. {len(issues)} issue(s):")
        for issue in issues:
            print(f"  #{issue['number']}: {issue['title']} → {issue['branch']}")

        semaphore = asyncio.Semaphore(_max_parallel)

        results = await asyncio.gather(
            *[
                run_issue(
                    i,
                    env,
                    repo_root,
                    _stage_overrides,
                    semaphore,
                    run_agent=_run_agent,
                    sha=_safe_sha,
                )
                for i in issues
            ],
            return_exceptions=True,
        )

        completed: list[dict] = []
        for issue, result in zip(issues, results):
            if isinstance(result, PreflightError):
                print(f"  ✗ #{issue['number']} ({issue['branch']}) pre-flight failed:")
                for check_name, command, output in result.failures:
                    print(f"    ✗ {check_name} ({command}): {output}")
            elif isinstance(result, Exception):
                tb = "".join(
                    traceback.format_exception(
                        type(result), result, result.__traceback__
                    )
                )
                timestamp = datetime.now(timezone.utc).isoformat()
                entry = f"--- {timestamp} ---\n{tb}\n"
                print(entry, file=sys.stderr)
                _logs_dir.mkdir(parents=True, exist_ok=True)
                with open(_logs_dir / "errors.log", "a", encoding="utf-8") as f:
                    f.write(entry)
                print(f"  ✗ #{issue['number']} ({issue['branch']}) failed: {result}")
            elif result is not None:
                completed.append(issue)

        if not completed:
            print("No commits produced. Nothing to merge.")
            continue

        print(f"\nExecution complete. {len(completed)} branch(es) with commits:")
        for i in completed:
            print(f"  {i['branch']}")

        github_svc = github_service or GithubService(repo=_get_repo(repo_root))

        await wait_for_clean_working_tree(repo_root, git_svc)

        conflict_issues: list[dict] = []
        for issue in completed:
            if git_svc.try_merge(repo_root, issue["branch"]):
                github_svc.close_issue_with_parents(issue["number"])
            else:
                conflict_issues.append(issue)

        clean_branches = [i["branch"] for i in completed if i not in conflict_issues]
        delete_merged_branches(clean_branches, repo_root, git_svc)

        clean_count = len(completed) - len(conflict_issues)
        if clean_count > 0 and not conflict_issues:
            check_failures = _run_host_checks(PREFLIGHT_CHECKS)
            if check_failures:
                bug_prompt = PROMPTS_DIR / "preflight-issue.md"
                await asyncio.gather(
                    *[
                        _run_agent(
                            name=f"bug-report ({check_name})",
                            prompt_file=bug_prompt,
                            mount_path=repo_root,
                            env=env,
                            prompt_args={
                                "CHECK_NAME": f"[post-merge] {check_name}",
                                "COMMAND": command,
                                "OUTPUT": output,
                            },
                            skip_preflight=True,
                        )
                        for check_name, command, output in check_failures
                    ]
                )
                continue
            _safe_sha = git_svc.get_head_sha(repo_root)
            _skip_preflight = True

        if conflict_issues:
            merge_stage = _stage_overrides.get("merge", {})
            await _run_agent(
                name="Merger",
                prompt_file=PROMPTS_DIR / "merge-prompt.md",
                mount_path=repo_root,
                env=env,
                prompt_args={
                    "BRANCHES": "\n".join(f"- {i['branch']}" for i in conflict_issues),
                    "ISSUES": "\n".join(
                        f"- #{i['number']}: {i['title']}" for i in conflict_issues
                    ),
                    "CHECKS": " && ".join(cmd for _, cmd in PREFLIGHT_CHECKS),
                },
                model=merge_stage.get("model", ""),
                effort=merge_stage.get("effort", ""),
                stage="pre-merge",
            )
            print("\nBranches merged.")
            delete_merged_branches(
                [i["branch"] for i in conflict_issues], repo_root, git_svc
            )

    print("\nAll done.")
