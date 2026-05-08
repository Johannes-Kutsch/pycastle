from pathlib import Path
from typing import Protocol

from ..agent_output_protocol import AgentOutput, AgentRole, NoCandidateOutput
from ..agent_result import PreflightFailure
from ..agent_runner import AgentRunnerProtocol, RunRequest
from ..config import Config
from ..session_resume import derived_session_uuid
from ..services import GitService
from ..status_display import StatusDisplay
from ..worktree import managed_worktree
from ._rows import phase_row

IMPROVE_SANDBOX = "pycastle/improve-sandbox"
_PHASE_PROGRESS_FILE = "_phase_progress"
_SID_PHASES = frozenset({"02-prd.md", "03-issues.md", "04-no-candidate-report.md"})


def next_prompt(
    last_completed_id: str | None, *, no_candidate_report: bool
) -> str | None:
    """Pure transition function: last completed phase ID → next prompt filename."""
    if last_completed_id is None:
        return "01-scan.md"
    if last_completed_id == "01-scan:picked":
        return "02-prd.md"
    if last_completed_id == "01-scan:no-candidate":
        return "04-no-candidate-report.md" if no_candidate_report else None
    if last_completed_id == "02-prd":
        return "03-issues.md"
    # 03-issues, 04-report, or unrecognised → terminal
    return None


def _phase_id(prompt_name: str, output: AgentOutput) -> str:
    if prompt_name == "01-scan.md":
        return (
            "01-scan:no-candidate"
            if isinstance(output, NoCandidateOutput)
            else "01-scan:picked"
        )
    return {
        "02-prd.md": "02-prd",
        "03-issues.md": "03-issues",
        "04-no-candidate-report.md": "04-report",
    }.get(prompt_name, prompt_name)


def _read_progress(progress_file: Path) -> str | None:
    try:
        value = progress_file.read_text(encoding="utf-8").strip()
        return value or None
    except OSError:
        return None


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
            short_sid = derived_session_uuid(AgentRole.IMPROVE, sandbox_path).split(
                "-"
            )[0]
            role_session_dir = (
                sandbox_path / ".pycastle-session" / AgentRole.IMPROVE.value
            )
            progress_file = role_session_dir / _PHASE_PROGRESS_FILE

            last_id = _read_progress(progress_file)
            prompt_name = next_prompt(
                last_id,
                no_candidate_report=deps.cfg.improve_no_candidate_report,
            )

            while prompt_name is not None:
                prompt_args = (
                    {"IMPROVE_SHORT_SID": short_sid}
                    if prompt_name in _SID_PHASES
                    else None
                )
                output = await deps.agent_runner.run(
                    RunRequest(
                        name="Improve Agent",
                        prompt_file=deps.cfg.prompts_dir / prompt_name,
                        mount_path=sandbox_path,
                        role=AgentRole.IMPROVE,
                        skip_preflight=True,
                        prompt_args=prompt_args,
                        model=deps.cfg.improve_override.model,
                        effort=deps.cfg.improve_override.effort,
                        stage="improve-sandbox",
                        status_display=deps.status_display,
                        work_body="Scanning for improvements",
                    )
                )

                assert not isinstance(output, PreflightFailure)
                completed_id = _phase_id(prompt_name, output)
                role_session_dir.mkdir(parents=True, exist_ok=True)
                progress_file.write_text(completed_id, encoding="utf-8")

                prompt_name = next_prompt(
                    completed_id,
                    no_candidate_report=deps.cfg.improve_no_candidate_report,
                )

        row.close("finished")
