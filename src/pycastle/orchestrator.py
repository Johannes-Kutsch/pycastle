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
from .container_runner import run_agent
from .errors import PreflightError
from .git_service import GitCommandError, GitService
from .github_service import GithubService
from .validate import validate_config


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
    if "issues" not in data:
        raise RuntimeError(f"Plan JSON has no 'issues' key. Keys found: {list(data.keys())}")
    return data["issues"]


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


def _run_host_checks(
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
) -> dict | None:
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
            return await run_agent(**kwargs)

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


async def run(env: dict[str, str], repo_root: Path) -> None:
    validate_config(STAGE_OVERRIDES)
    prune_orphan_worktrees(repo_root)
    for iteration in range(1, MAX_ITERATIONS + 1):
        print(f"\n=== Iteration {iteration}/{MAX_ITERATIONS} ===\n")

        plan_stage = STAGE_OVERRIDES.get("plan", {})
        try:
            plan_output = await run_agent(
                name="Planner",
                prompt_file=PROMPTS_DIR / "plan-prompt.md",
                mount_path=repo_root,
                env=env,
                prompt_args={"ISSUE_LABEL": ISSUE_LABEL},
                model=plan_stage.get("model", ""),
                effort=plan_stage.get("effort", ""),
                stage="pre-planning",
            )
        except PreflightError as exc:
            print("[Planner] Pre-flight failed — aborting run:")
            for check_name, command, output in exc.failures:
                print(f"  ✗ {check_name} ({command}): {output}")
            return
        issues = parse_plan(plan_output)

        if not issues:
            print("No issues to work on. Exiting.")
            break

        print(f"Planning complete. {len(issues)} issue(s):")
        for issue in issues:
            print(f"  #{issue['number']}: {issue['title']} → {issue['branch']}")

        semaphore = asyncio.Semaphore(MAX_PARALLEL)

        results = await asyncio.gather(
            *[run_issue(i, env, repo_root, STAGE_OVERRIDES, semaphore) for i in issues],
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
                LOGS_DIR.mkdir(parents=True, exist_ok=True)
                with open(LOGS_DIR / "errors.log", "a", encoding="utf-8") as f:
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

        git_svc = GitService()
        github_svc = GithubService(repo=_get_repo(repo_root))

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
                bug_prompt = PROMPTS_DIR / "bug-report.md"
                await asyncio.gather(
                    *[
                        run_agent(
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

        if conflict_issues:
            merge_stage = STAGE_OVERRIDES.get("merge", {})
            await run_agent(
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
