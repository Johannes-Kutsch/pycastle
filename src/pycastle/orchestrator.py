import asyncio
import json
import re
import shutil
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .config import ISSUE_LABEL, LOGS_DIR, MAX_ITERATIONS, MAX_PARALLEL, PROMPTS_DIR
from .container_runner import run_agent


def prune_orphan_worktrees(repo_root: Path) -> None:
    worktrees_dir = repo_root / "pycastle" / ".worktrees"
    if not worktrees_dir.exists():
        return
    raw = subprocess.check_output(
        ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
        text=True,
    )
    active = {
        line[len("worktree ") :].strip()
        for line in raw.splitlines()
        if line.startswith("worktree ")
    }
    for child in worktrees_dir.iterdir():
        if str(child.resolve()) not in active and child.is_dir():
            shutil.rmtree(child)


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
    return json.loads(match.group(1))["issues"]


async def run_issue(issue: dict, env: dict[str, str], repo_root: Path) -> dict | None:
    prompt_args = {
        "ISSUE_NUMBER": str(issue["number"]),
        "ISSUE_TITLE": issue["title"],
        "BRANCH": issue["branch"],
    }
    result = await run_agent(
        name=f"Implementer #{issue['number']}",
        prompt_file=PROMPTS_DIR / "implement-prompt.md",
        mount_path=repo_root,
        env=env,
        prompt_args=prompt_args,
        branch=issue["branch"],
    )
    if "<promise>COMPLETE</promise>" not in _extract_text(result):
        return None
    await run_agent(
        name=f"Reviewer #{issue['number']}",
        prompt_file=PROMPTS_DIR / "review-prompt.md",
        mount_path=repo_root,
        env=env,
        prompt_args=prompt_args,
        branch=issue["branch"],
    )
    return issue


async def run(env: dict[str, str], repo_root: Path) -> None:
    prune_orphan_worktrees(repo_root)
    for iteration in range(1, MAX_ITERATIONS + 1):
        print(f"\n=== Iteration {iteration}/{MAX_ITERATIONS} ===\n")

        plan_output = await run_agent(
            name="Planner",
            prompt_file=PROMPTS_DIR / "plan-prompt.md",
            mount_path=repo_root,
            env=env,
            prompt_args={"ISSUE_LABEL": ISSUE_LABEL},
        )
        issues = parse_plan(plan_output)

        if not issues:
            print("No issues to work on. Exiting.")
            break

        print(f"Planning complete. {len(issues)} issue(s):")
        for issue in issues:
            print(f"  #{issue['number']}: {issue['title']} → {issue['branch']}")

        semaphore = asyncio.Semaphore(MAX_PARALLEL)

        async def bounded(issue: dict) -> dict | None:
            async with semaphore:
                return await run_issue(issue, env, repo_root)

        results = await asyncio.gather(
            *[bounded(i) for i in issues], return_exceptions=True
        )

        completed: list[dict] = []
        for issue, result in zip(issues, results):
            if isinstance(result, Exception):
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

        await run_agent(
            name="Merger",
            prompt_file=PROMPTS_DIR / "merge-prompt.md",
            mount_path=repo_root,
            env=env,
            prompt_args={
                "BRANCHES": "\n".join(f"- {i['branch']}" for i in completed),
                "ISSUES": "\n".join(
                    f"- #{i['number']}: {i['title']}" for i in completed
                ),
            },
        )
        print("\nBranches merged.")

    print("\nAll done.")
