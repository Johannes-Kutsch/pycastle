import dataclasses
import re
from pathlib import Path
from typing import Protocol, TypeAlias

from ..agent_output_protocol import (
    AgentOutputProtocolError,
    AgentRole,
    IssueOutput,
)
from ..agent_result import PreflightFailure
from ..agent_runner import AgentRunnerProtocol, RunRequest
from ..config import Config
from ..services import GitCommandError, GitService, GithubService
from ..status_display import StatusDisplay
from ..worktree import detached_worktree
from ._utils import _wait_for_clean_working_tree


class _PreflightDeps(Protocol):
    git_svc: GitService
    github_svc: GithubService
    cfg: Config
    status_display: StatusDisplay
    agent_runner: AgentRunnerProtocol
    repo_root: Path


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
class PreflightReady:
    sha: str
    issues: list[dict]


@dataclasses.dataclass(frozen=True)
class PreflightHITL:
    worktree_sha: str
    issue_number: int


@dataclasses.dataclass(frozen=True)
class PreflightAFK:
    worktree_sha: str
    issues: list[dict]


PreflightResult: TypeAlias = PreflightReady | PreflightHITL | PreflightAFK


async def handle_preflight_failure(
    failures: tuple[tuple[str, str, str], ...],
    deps: _PreflightDeps,
    mount_path: Path,
) -> tuple[str, int]:
    check_name, command, output = failures[0]
    agent_result = await deps.agent_runner.run(
        RunRequest(
            name="Pre-Flight Reporter",
            prompt_file=deps.cfg.prompts_dir / "preflight-issue.md",
            mount_path=mount_path,
            role=AgentRole.PREFLIGHT_ISSUE,
            prompt_args={
                "CHECK_NAME": check_name,
                "COMMAND": command,
                "OUTPUT": output,
                "BUG_LABEL": deps.cfg.bug_label,
                "ISSUE_LABEL": deps.cfg.issue_label,
                "HITL_LABEL": deps.cfg.hitl_label,
            },
            skip_preflight=True,
            status_display=deps.status_display,
            work_body=f"reporting {check_name} issue",
        )
    )
    if isinstance(agent_result, PreflightFailure):
        raise RuntimeError(
            "preflight-issue agent returned a PreflightFailure unexpectedly"
        )
    if not isinstance(agent_result, IssueOutput):
        raise RuntimeError(
            f"Preflight-issue agent returned unexpected output type: {type(agent_result).__name__}"
        )
    if deps.cfg.hitl_label in agent_result.labels:
        return "hitl", agent_result.number
    return "afk", agent_result.number


async def preflight_phase(deps: _PreflightDeps) -> PreflightResult:
    await _wait_for_clean_working_tree(deps, "Preflight")
    try:
        deps.git_svc.pull(deps.repo_root)
    except GitCommandError:
        deps.status_display.print(
            "Preflight",
            "git pull --ff-only failed — remote branch has diverged or is unreachable. "
            "Resolve manually and retry.",
            style="error",
        )
        raise
    sha = deps.git_svc.get_head_sha(deps.repo_root)
    open_issues = strip_stale_blocker_refs(
        deps.github_svc.get_open_issues(deps.cfg.issue_label)
    )
    if not open_issues:
        return PreflightReady(sha=sha, issues=[])

    async with detached_worktree("pre-flight-sandbox", sha, deps) as wt:
        failures = await deps.agent_runner.run_preflight(
            name="Preflight Agent",
            mount_path=wt,
            stage="PREFLIGHT",
            status_display=deps.status_display,
            work_body="Checking",
        )

        if failures:
            try:
                verdict, pf_num = await handle_preflight_failure(
                    tuple(failures), deps, wt
                )
            except AgentOutputProtocolError as parse_exc:
                raise RuntimeError(str(parse_exc)) from parse_exc
            if verdict == "hitl":
                return PreflightHITL(worktree_sha=sha, issue_number=pf_num)
            pf_title = deps.github_svc.get_issue_title(pf_num)
            return PreflightAFK(
                worktree_sha=sha, issues=[{"number": pf_num, "title": pf_title}]
            )

        return PreflightReady(sha=sha, issues=open_issues)
