import shutil
from pathlib import Path
from typing import Protocol

from ..agent_output_protocol import AgentOutput, AgentRole, NoCandidateOutput
from ..agent_result import PreflightFailure
from ..agent_runner import AgentRunnerProtocol, RunRequest
from ..config import Config
from ..prompt_pipeline import load_standards
from ..services import GitService
from ..session_resume import derived_session_uuid
from ..status_display import StatusDisplay
from ..worktree import managed_worktree
from ._rows import phase_row

IMPROVE_SANDBOX = "pycastle/improve-sandbox"
_PHASE_PROGRESS_FILE = "_phase_progress"
_PHASE_IN_FLIGHT_FILE = "_phase_in_flight"
_SID_PHASES = frozenset({"02-prd.md", "03-issues.md", "04-no-candidate-report.md"})
_STANDARDS_PHASES = frozenset({"01-scan.md"})
_VALID_PHASE_IDS = frozenset(
    {"01-scan:picked", "01-scan:no-candidate", "02-prd", "03-issues", "04-report"}
)
_PHASE_DISPLAY: dict[str, tuple[str, str]] = {
    "01-scan.md": ("Scan Agent", "picking an improvement"),
    "02-prd.md": ("PRD Agent", "writing PRD"),
    "03-issues.md": ("Slice Agent", "filing sub-issues"),
    "04-no-candidate-report.md": (
        "Rejection Report Agent",
        "filing no-candidate report",
    ),
}


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
        return value if value in _VALID_PHASE_IDS else None
    except OSError:
        return None


class _ImproveDeps(Protocol):
    cfg: Config
    status_display: StatusDisplay
    agent_runner: AgentRunnerProtocol
    repo_root: Path
    git_svc: GitService


async def improve_phase(deps: _ImproveDeps, *, sha: str) -> None:
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
            in_flight_file = role_session_dir / _PHASE_IN_FLIGHT_FILE

            last_id = _read_progress(progress_file)
            in_flight_id = (
                in_flight_file.read_text(encoding="utf-8").strip()
                if in_flight_file.is_file()
                else None
            )
            prompt_name = next_prompt(
                last_id,
                no_candidate_report=deps.cfg.improve_no_candidate_report,
            )
            while prompt_name is not None:
                if prompt_name in _STANDARDS_PHASES:
                    prompt_args = load_standards(deps.cfg.prompts_dir)
                elif prompt_name in _SID_PHASES:
                    prompt_args = {"IMPROVE_SHORT_SID": short_sid}
                else:
                    prompt_args = None
                display_name, display_body = _PHASE_DISPLAY[prompt_name]
                phase_key = prompt_name.removesuffix(".md")
                is_mid_phase_retry = in_flight_id == phase_key
                role_session_dir.mkdir(parents=True, exist_ok=True)
                in_flight_file.write_text(phase_key, encoding="utf-8")
                output = await deps.agent_runner.run(
                    RunRequest(
                        name=display_name,
                        prompt_file=deps.cfg.prompts_dir / "improve" / prompt_name,
                        mount_path=sandbox_path,
                        role=AgentRole.IMPROVE,
                        skip_preflight=True,
                        prompt_args=prompt_args,
                        model=deps.cfg.improve_override.model,
                        effort=deps.cfg.improve_override.effort,
                        stage="improve-sandbox",
                        status_display=deps.status_display,
                        work_body=display_body,
                        send_role_prompt_on_resume=last_id is not None
                        and not is_mid_phase_retry,
                    )
                )

                assert not isinstance(output, PreflightFailure)
                completed_id = _phase_id(prompt_name, output)
                role_session_dir.mkdir(parents=True, exist_ok=True)
                progress_file.write_text(completed_id, encoding="utf-8")
                in_flight_file.unlink(missing_ok=True)
                last_id = completed_id
                in_flight_id = None

                prompt_name = next_prompt(
                    completed_id,
                    no_candidate_report=deps.cfg.improve_no_candidate_report,
                )

            shutil.rmtree(role_session_dir, ignore_errors=True)

        row.close("finished")
