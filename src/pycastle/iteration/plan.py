import dataclasses
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


async def handle_preflight_failure(
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
        status_display=deps.status_display,
    )
    if isinstance(agent_result, PreflightFailure):
        raise RuntimeError(
            "preflight-issue agent returned a PreflightFailure unexpectedly"
        )
    issue_output = parse(agent_result, AgentRole.PREFLIGHT_ISSUE)
    if deps.cfg.hitl_label in issue_output.labels:
        return "hitl", issue_output.number
    return "afk", issue_output.number


