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
from collections.abc import Sequence
from typing import Any

from .config import Config, config as _cfg
from .container_runner import run_agent as _default_run_agent
from .errors import PreflightError, UsageLimitError
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


def branch_for(issue_number: int) -> str:
    return f"sandcastle/issue-{issue_number}"


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


def parse_plan(output: str) -> list[dict]:
    text = _extract_text(output)
    match = re.search(r"<plan>([\s\S]*?)</plan>", text)
    if not match:
        raise RuntimeError("Planner produced no <plan> tag.\n\n" + text)
    data = json.loads(match.group(1))
    if "unblocked_issues" in data:
        raw = data["unblocked_issues"]
    elif "issues" in data:
        raw = data["issues"]
    else:
        raise RuntimeError(
            f"Plan JSON has no 'unblocked_issues' or 'issues' key. Keys found: {list(data.keys())}"
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


def _run_host_checks_impl(
    checks: Sequence[tuple[str, str]],
) -> list[tuple[str, str, str]]:
    failures = []
    for check_name, command in checks:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            failures.append((check_name, command, result.stdout + result.stderr))
    return failures


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
) -> tuple[str, int]:
    """Spawn preflight-issue agent for the first failing check; returns ('hitl'|'afk', issue_number)."""
    check_name, command, output = failures[0]
    agent_output = await run_agent(
        name=f"preflight-issue ({check_name})",
        prompt_file=prompts_dir / "preflight-issue.md",
        mount_path=repo_root,
        env=env,
        prompt_args={"CHECK_NAME": check_name, "COMMAND": command, "OUTPUT": output},
        skip_preflight=True,
    )
    text = _extract_text(agent_output)
    match = re.search(r"<issue>(\d+)</issue>", text)
    if not match:
        raise RuntimeError(
            "preflight-issue agent produced no <issue>NUMBER</issue> tag.\n\n" + text
        )
    issue_number = int(match.group(1))
    labels = github_svc.get_labels(issue_number)
    if hitl_label in labels:
        return "hitl", issue_number
    return "afk", issue_number


async def run_issue(
    issue: dict,
    env: dict[str, str],
    repo_root: Path,
    semaphore: asyncio.Semaphore | None = None,
    *,
    cfg: Config | None = None,
    run_agent: Any | None = None,
    sha: str | None = None,
) -> dict | None:
    cfg = cfg if cfg is not None else _cfg
    _run_agent = run_agent or _default_run_agent
    _branch = branch_for(issue["number"])
    prompt_args = {
        "ISSUE_NUMBER": str(issue["number"]),
        "ISSUE_TITLE": issue["title"],
        "BRANCH": _branch,
        "FEEDBACK_COMMANDS": _format_feedback_commands(cfg.implement_checks),
    }

    async def _bounded_run_agent(**kwargs: Any) -> str:
        async with semaphore or contextlib.nullcontext():
            return await _run_agent(**kwargs)

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
    if "<promise>COMPLETE</promise>" not in _extract_text(result):
        return None
    reviewer_prompt_args = {
        "ISSUE_NUMBER": str(issue["number"]),
        "ISSUE_TITLE": issue["title"],
        "BRANCH": _branch,
        "FEEDBACK_COMMANDS": _format_feedback_commands(cfg.implement_checks),
    }
    await _bounded_run_agent(
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
    cfg: Config | None = None,
) -> None:
    cfg = cfg if cfg is not None else _cfg
    _run_agent = run_agent or _default_run_agent
    _validate_config = validate_config or _default_validate_config
    _run_host_checks = run_host_checks or _run_host_checks_impl

    _validate_config(
        {
            "plan": {
                "model": cfg.plan_override.model,
                "effort": cfg.plan_override.effort,
            },
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
    )
    prune_orphan_worktrees(repo_root)
    git_svc = git_service or GitService()
    _safe_sha: str | None = None
    _skip_preflight: bool = False
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

        issues: list[dict] | None = None
        try:
            plan_output = await _run_agent(
                name="Planner",
                prompt_file=cfg.prompts_dir / "plan-prompt.md",
                mount_path=repo_root,
                env=env,
                prompt_args={
                    "OPEN_ISSUES_JSON": json.dumps(
                        strip_stale_blocker_refs(
                            _get_github_svc().get_open_issues(cfg.issue_label)
                        )
                    )
                },
                model=cfg.plan_override.model,
                effort=cfg.plan_override.effort,
                stage="pre-planning",
                skip_preflight=_skip_preflight,
            )
        except PreflightError as exc:
            verdict, pf_num = await _handle_preflight_failure(
                exc.failures,
                env,
                repo_root,
                _get_github_svc(),
                _run_agent,
                cfg.hitl_label,
                cfg.prompts_dir,
            )
            if verdict == "hitl":
                print(
                    f"Preflight issue #{pf_num} requires human intervention. Exiting."
                )
                sys.exit(1)
            pf_title = _get_github_svc().get_issue_title(pf_num)
            issues = [
                {
                    "number": pf_num,
                    "title": pf_title,
                }
            ]
            _skip_preflight = True  # skip SHA pinning — code was broken

        if issues is None:
            issues = parse_plan(plan_output)

        if not issues:
            print(f"No issues with label '{cfg.issue_label}' found. Skipping.")
            break

        if not _skip_preflight:
            _safe_sha = git_svc.get_head_sha(repo_root)
        _skip_preflight = False

        print(f"Planning complete. {len(issues)} issue(s):")
        for issue in issues:
            print(
                f"  #{issue['number']}: {issue['title']} → {branch_for(issue['number'])}"
            )

        semaphore = asyncio.Semaphore(cfg.max_parallel)

        results = await asyncio.gather(
            *[
                run_issue(
                    i,
                    env,
                    repo_root,
                    semaphore,
                    cfg=cfg,
                    run_agent=_run_agent,
                    sha=_safe_sha,
                )
                for i in issues
            ],
            return_exceptions=True,
        )

        if any(isinstance(r, UsageLimitError) for r in results):
            print(
                "Usage limit reached. Worktrees preserved. Run 'pycastle run' again to resume.",
                file=sys.stderr,
            )
            sys.exit(1)

        completed: list[dict] = []
        for issue, result in zip(issues, results):
            if isinstance(result, PreflightError):
                print(
                    f"  ✗ #{issue['number']} ({branch_for(issue['number'])}) pre-flight failed:"
                )
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
                cfg.logs_dir.mkdir(parents=True, exist_ok=True)
                with open(cfg.logs_dir / "errors.log", "a", encoding="utf-8") as f:
                    f.write(entry)
                print(
                    f"  ✗ #{issue['number']} ({branch_for(issue['number'])}) failed: {result}"
                )
            elif result is not None:
                completed.append(issue)

        if not completed:
            print("No commits produced. Nothing to merge.")
            continue

        print(f"\nExecution complete. {len(completed)} branch(es) with commits:")
        for i in completed:
            print(f"  {branch_for(i['number'])}")

        await wait_for_clean_working_tree(repo_root, git_svc)

        conflict_issues: list[dict] = []
        for issue in completed:
            if git_svc.try_merge(repo_root, branch_for(issue["number"])):
                _get_github_svc().close_issue(issue["number"])
            else:
                conflict_issues.append(issue)
        if len(completed) > len(conflict_issues):
            _get_github_svc().close_completed_parent_issues()

        clean_branches = [
            branch_for(i["number"]) for i in completed if i not in conflict_issues
        ]
        delete_merged_branches(clean_branches, repo_root, git_svc)

        clean_count = len(completed) - len(conflict_issues)
        if clean_count > 0 and not conflict_issues:
            check_failures = _run_host_checks(cfg.preflight_checks)
            if check_failures:
                verdict, pf_num = await _handle_preflight_failure(
                    check_failures,
                    env,
                    repo_root,
                    _get_github_svc(),
                    _run_agent,
                    cfg.hitl_label,
                    cfg.prompts_dir,
                )
                if verdict == "hitl":
                    print(
                        f"Preflight issue #{pf_num} requires human intervention. Exiting."
                    )
                    sys.exit(1)
                pf_title = _get_github_svc().get_issue_title(pf_num)
                pf_issue = {
                    "number": pf_num,
                    "title": pf_title,
                }
                pf_semaphore = asyncio.Semaphore(cfg.max_parallel)
                try:
                    pf_completed = await run_issue(
                        pf_issue,
                        env,
                        repo_root,
                        pf_semaphore,
                        cfg=cfg,
                        run_agent=_run_agent,
                        sha=None,
                    )
                except UsageLimitError:
                    print(
                        "Usage limit reached. Worktrees preserved. Run 'pycastle run' again to resume.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                if pf_completed:
                    pf_branch = branch_for(pf_num)
                    await wait_for_clean_working_tree(repo_root, git_svc)
                    if git_svc.try_merge(repo_root, pf_branch):
                        _get_github_svc().close_issue(pf_num)
                        _get_github_svc().close_completed_parent_issues()
                        delete_merged_branches([pf_branch], repo_root, git_svc)
                        second_failures = _run_host_checks(cfg.preflight_checks)
                        if not second_failures:
                            _safe_sha = git_svc.get_head_sha(repo_root)
                            _skip_preflight = True
                continue
            _safe_sha = git_svc.get_head_sha(repo_root)
            _skip_preflight = True

        if conflict_issues:
            await _run_agent(
                name="Merger",
                prompt_file=cfg.prompts_dir / "merge-prompt.md",
                mount_path=repo_root,
                env=env,
                prompt_args={
                    "BRANCHES": "\n".join(
                        f"- {branch_for(i['number'])}" for i in conflict_issues
                    ),
                    "CHECKS": " && ".join(cmd for _, cmd in cfg.preflight_checks),
                },
                model=cfg.merge_override.model,
                effort=cfg.merge_override.effort,
                stage="pre-merge",
            )
            print("\nBranches merged.")
            delete_merged_branches(
                [branch_for(i["number"]) for i in conflict_issues], repo_root, git_svc
            )
            for issue in conflict_issues:
                _get_github_svc().close_issue(issue["number"])
            _get_github_svc().close_completed_parent_issues()

    print("\nAll done.")
