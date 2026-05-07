from pathlib import Path
from typing import Protocol

from ..agent_output_protocol import AgentRole
from ..agent_runner import AgentRunnerProtocol, RunRequest
from ..config import Config
from ..services import GitService
from ..status_display import StatusDisplay
from ..worktree import managed_worktree
from ._rows import phase_row

IMPROVE_SANDBOX = "pycastle/improve-sandbox"


class _ImproveDeps(Protocol):
    cfg: Config
    status_display: StatusDisplay
    agent_runner: AgentRunnerProtocol
    repo_root: Path
    git_svc: GitService


async def improve_phase(deps: _ImproveDeps) -> None:
    sha = deps.git_svc.get_head_sha(deps.repo_root)
    async with phase_row(
        deps.status_display, "Improve", initial_phase="Running"
    ) as row:
        async with managed_worktree(
            "improve-sandbox",
            branch=IMPROVE_SANDBOX,
            sha=sha,
            delete_branch_on_teardown=True,
            deps=deps,
        ) as sandbox_path:
            await deps.agent_runner.run(
                RunRequest(
                    name="Improve Agent",
                    prompt_file=deps.cfg.prompts_dir / "improve-prompt.md",
                    mount_path=sandbox_path,
                    role=AgentRole.IMPROVE,
                    skip_preflight=True,
                    model=deps.cfg.improve_override.model,
                    effort=deps.cfg.improve_override.effort,
                    stage="improve-sandbox",
                    status_display=deps.status_display,
                    work_body="Scanning for improvements",
                )
            )
        row.close("finished")
