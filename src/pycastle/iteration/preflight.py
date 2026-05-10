import asyncio
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
from ..prompt_pipeline import PromptTemplate
from ..services import GitCommandError, GitService, GithubService
from ..status_display import StatusDisplay
from ._utils import _wait_for_clean_working_tree


@dataclasses.dataclass(frozen=True)
class PreflightReady:
    sha: str


@dataclasses.dataclass(frozen=True)
class PreflightHITL:
    sha: str
    issue_number: int


@dataclasses.dataclass(frozen=True)
class PreflightAFK:
    sha: str
    issue_number: int


PreflightResult: TypeAlias = PreflightReady | PreflightHITL | PreflightAFK


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


class PreflightCache:
    """Single-slot, process-scoped cache for preflight verdicts.

    Constructed once in orchestrator.run() outside the iteration loop so its slot
    survives iteration reconstruction.  All callers serialise via the internal lock.
    """

    def __init__(self) -> None:
        self._verdict: PreflightResult | None = None
        self._lock: asyncio.Lock = asyncio.Lock()

    async def get_safe_sha(self, deps: _PreflightDeps) -> PreflightResult:
        from ..worktree import transient_worktree

        async with self._lock:
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

            if self._verdict is not None and self._verdict.sha == sha:
                return self._verdict

            async with transient_worktree(
                "preflight-sandbox", sha=sha, deps=deps
            ) as mount_path:
                failures = await deps.agent_runner.run_preflight(
                    name="Preflight Agent",
                    mount_path=mount_path,
                    stage="PREFLIGHT",
                    status_display=deps.status_display,
                    work_body="Checking",
                )

                if failures:
                    try:
                        verdict, pf_num = await handle_preflight_failure(
                            tuple(failures), deps, mount_path
                        )
                    except AgentOutputProtocolError as parse_exc:
                        raise RuntimeError(str(parse_exc)) from parse_exc
                    if verdict == "hitl":
                        result: PreflightResult = PreflightHITL(
                            sha=sha, issue_number=pf_num
                        )
                    else:
                        result = PreflightAFK(sha=sha, issue_number=pf_num)
                else:
                    result = PreflightReady(sha=sha)

                self._verdict = result
                return result
