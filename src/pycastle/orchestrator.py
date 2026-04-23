import asyncio
import json
import re
from pathlib import Path

from .config import ISSUE_LABEL, MAX_ITERATIONS, MAX_PARALLEL, PROMPTS_DIR
from .container_runner import run_agent


def parse_plan(output: str) -> list[dict]:
    match = re.search(r"<plan>([\s\S]*?)</plan>", output)
    if not match:
        raise RuntimeError("Planner produced no <plan> tag.\n\n" + output)
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
    if "<promise>COMPLETE</promise>" not in result:
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
                "ISSUES": "\n".join(f"- #{i['number']}: {i['title']}" for i in completed),
            },
        )
        print("\nBranches merged.")

    print("\nAll done.")
