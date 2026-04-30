import dataclasses
import json
import re
from pathlib import Path
from typing import TypeAlias

from ..agent_output_protocol import (
    AgentOutputProtocolError,
    AgentRole,
    parse,
)
from ..agent_result import PreflightFailure
from ._deps import Deps


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


@dataclasses.dataclass(frozen=True)
class PlanReady:
    worktree_sha: str
    issues: list[dict]


@dataclasses.dataclass(frozen=True)
class PlanHITL:
    worktree_sha: str
    issue_number: int


@dataclasses.dataclass(frozen=True)
class PlanAFK:
    worktree_sha: str
    issues: list[dict]


PlanResult: TypeAlias = PlanReady | PlanHITL | PlanAFK


async def _handle_preflight_failure(
    failures: tuple[tuple[str, str, str], ...],
    deps: Deps,
    mount_path: Path,
) -> tuple[str, int]:
    check_name, command, output = failures[0]
    agent_result = await deps.agent_runner.run(
        name=f"preflight-issue ({check_name})",
        prompt_file=deps.cfg.prompts_dir / "preflight-issue.md",
        mount_path=mount_path,
        prompt_args={
            "CHECK_NAME": check_name,
            "COMMAND": command,
            "OUTPUT": output,
            "BUG_LABEL": deps.cfg.bug_label,
            "ISSUE_LABEL": deps.cfg.issue_label,
            "HITL_LABEL": deps.cfg.hitl_label,
        },
        skip_preflight=True,
    )
    if isinstance(agent_result, PreflightFailure):
        raise RuntimeError(
            "preflight-issue agent returned a PreflightFailure unexpectedly"
        )
    issue_output = parse(agent_result, AgentRole.PREFLIGHT_ISSUE)
    if deps.cfg.hitl_label in issue_output.labels:
        return "hitl", issue_output.number
    return "afk", issue_output.number


async def plan_phase(deps: Deps) -> PlanResult:
    sha = deps.git_svc.get_head_sha(deps.repo_root)
    open_issues = strip_stale_blocker_refs(
        deps.github_svc.get_open_issues(deps.cfg.issue_label)
    )
    if not open_issues:
        return PlanReady(worktree_sha=sha, issues=[])

    worktree_path = (
        deps.repo_root / deps.cfg.pycastle_dir / ".worktrees" / "plan-sandbox"
    )
    deps.git_svc.checkout_detached(deps.repo_root, worktree_path, sha)

    try:
        raw = await deps.agent_runner.run(
            name="Planner",
            prompt_file=deps.cfg.prompts_dir / "plan-prompt.md",
            mount_path=worktree_path,
            prompt_args={"OPEN_ISSUES_JSON": json.dumps(open_issues)},
            model=deps.cfg.plan_override.model,
            effort=deps.cfg.plan_override.effort,
            stage="plan-sandbox",
        )

        if isinstance(raw, PreflightFailure):
            try:
                verdict, pf_num = await _handle_preflight_failure(
                    raw.failures, deps, worktree_path
                )
            except AgentOutputProtocolError as parse_exc:
                raise RuntimeError(str(parse_exc)) from parse_exc
            if verdict == "hitl":
                return PlanHITL(worktree_sha=sha, issue_number=pf_num)
            pf_title = deps.github_svc.get_issue_title(pf_num)
            return PlanAFK(
                worktree_sha=sha, issues=[{"number": pf_num, "title": pf_title}]
            )

        try:
            planner_output = parse(raw, AgentRole.PLANNER)
        except AgentOutputProtocolError as exc:
            raise RuntimeError(str(exc)) from exc

        return PlanReady(
            worktree_sha=sha,
            issues=sorted(planner_output.issues, key=lambda i: i["number"]),
        )
    finally:
        deps.git_svc.remove_worktree(deps.repo_root, worktree_path)
