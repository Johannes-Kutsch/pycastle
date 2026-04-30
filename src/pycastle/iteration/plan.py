import dataclasses
import json
import re
from pathlib import Path
from typing import TypeAlias

from ..agent_output_protocol import IssueParseError, PlanParseError, parse_issue_number
from ..agent_output_protocol import parse_plan as _parse_plan
from ..agent_result import AgentIncomplete, AgentSuccess
from ..errors import PreflightError
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
    failures: list[tuple[str, str, str]],
    deps: Deps,
    mount_path: Path,
) -> tuple[str, int]:
    check_name, command, output = failures[0]
    agent_result = await deps.run_agent(
        name=f"preflight-issue ({check_name})",
        prompt_file=deps.cfg.prompts_dir / "preflight-issue.md",
        mount_path=mount_path,
        env=deps.env,
        prompt_args={"CHECK_NAME": check_name, "COMMAND": command, "OUTPUT": output},
        skip_preflight=True,
    )
    if isinstance(agent_result, AgentSuccess):
        raw_text = agent_result.output
    elif isinstance(agent_result, AgentIncomplete):
        raw_text = agent_result.partial_output
    else:
        raw_text = str(agent_result)
    _label, issue_number = parse_issue_number(raw_text)
    labels = deps.github_svc.get_labels(issue_number)
    if deps.cfg.hitl_label in labels:
        return "hitl", issue_number
    return "afk", issue_number


async def plan_phase(deps: Deps) -> PlanResult:
    sha = deps.git_svc.get_head_sha(deps.repo_root)
    open_issues = strip_stale_blocker_refs(
        deps.github_svc.get_open_issues(deps.cfg.issue_label)
    )
    if not open_issues:
        return PlanReady(worktree_sha=sha, issues=[])

    worktree_path = deps.repo_root / deps.cfg.pycastle_dir / ".worktrees" / "pre-planning"
    deps.git_svc.checkout_detached(deps.repo_root, worktree_path, sha)

    try:
        try:
            raw = await deps.run_agent(
                name="Planner",
                prompt_file=deps.cfg.prompts_dir / "plan-prompt.md",
                mount_path=worktree_path,
                env=deps.env,
                prompt_args={"OPEN_ISSUES_JSON": json.dumps(open_issues)},
                model=deps.cfg.plan_override.model,
                effort=deps.cfg.plan_override.effort,
                stage="pre-planning",
            )
        except PreflightError as exc:
            try:
                verdict, pf_num = await _handle_preflight_failure(exc.failures, deps, worktree_path)
            except IssueParseError as parse_exc:
                raise RuntimeError(str(parse_exc)) from parse_exc
            if verdict == "hitl":
                return PlanHITL(worktree_sha=sha, issue_number=pf_num)
            pf_title = deps.github_svc.get_issue_title(pf_num)
            return PlanAFK(worktree_sha=sha, issues=[{"number": pf_num, "title": pf_title}])

        if isinstance(raw, AgentSuccess):
            plan_text = raw.output
        elif isinstance(raw, AgentIncomplete):
            plan_text = raw.partial_output
        else:
            plan_text = str(raw)

        try:
            issues = _parse_plan(plan_text)
        except PlanParseError as exc:
            raise RuntimeError(str(exc)) from exc

        return PlanReady(worktree_sha=sha, issues=issues)
    finally:
        deps.git_svc.remove_worktree(deps.repo_root, worktree_path)
