import dataclasses
import re
from pathlib import Path
from typing import Protocol, TypeAlias

from ..agent_output_protocol import (
    AgentOutputProtocolError,
    AgentRole,
    IssueOutput,
)
from ..agent_runner import AgentRunnerProtocol, RunRequest
from ..config import Config
from ..errors import PycastleError
from ..prompt_pipeline import PromptTemplate
from ..services import GitCommandError, GitService, GithubService
from ..status_display import StatusDisplay
from ..worktree import transient_worktree
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


@dataclasses.dataclass(frozen=True)
class PreflightHITL:
    worktree_sha: str
    issue_number: int


PreflightResult: TypeAlias = PreflightReady | PreflightHITL


async def handle_preflight_failure(
    failures: tuple[tuple[str, str, str], ...],
    deps: _PreflightDeps,
    mount_path: Path,
) -> tuple[str, int]:
    check_name, command, output = failures[0]
    agent_result = await deps.agent_runner.run(
        RunRequest(
            name="Pre-Flight Reporter",
            template=PromptTemplate.PREFLIGHT_ISSUE,
            mount_path=mount_path,
            role=AgentRole.PREFLIGHT_ISSUE,
            scope_args={
                "CHECK_NAME": check_name,
                "COMMAND": command,
                "OUTPUT": output,
            },
            skip_preflight=True,
            model=deps.cfg.preflight_issue_override.model,
            effort=deps.cfg.preflight_issue_override.effort,
            status_display=deps.status_display,
            work_body=f"reporting {check_name} issue",
        )
    )
    if not isinstance(agent_result, IssueOutput):
        raise RuntimeError(
            f"Preflight-issue agent returned unexpected output type: {type(agent_result).__name__}"
        )
    if deps.cfg.hitl_label in agent_result.labels:
        return "hitl", agent_result.number
    return "afk", agent_result.number


async def preflight_phase(
    deps: _PreflightDeps,
    open_issues: list[dict],
    all_open_issues: list[dict],
) -> PreflightResult:
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
    async with transient_worktree("pre-flight-sandbox", sha=sha, deps=deps) as wt:
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
            raise PycastleError(
                f"Preflight check failed; fix issue #{pf_num} filed for triage."
            )

        return PreflightReady(sha=sha)
