import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pycastle.agents.output_protocol import AgentRole, IssueOutput
from pycastle.config import Config, StageOverride
from pycastle.diagnostic_reporter_dispatch import (
    DiagnosticReporterDispatchAFK,
    DiagnosticReporterDispatchHITL,
    DiagnosticReporterDispatchMountFallback,
    DiagnosticReporterDispatchValidationSkipped,
    run_diagnostic_reporter_dispatch,
)
from pycastle.prompts.dispatch import build_prompt_invocation
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.prompts.scope_args import build_preflight_scope_args


def _make_valid_mount(tmp_path: Path) -> Path:
    worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    return worktrees_dir / "reporter-sandbox"


def _make_rejected_mount(tmp_path: Path) -> Path:
    (tmp_path / "pycastle" / ".worktrees").mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside" / "reporter-sandbox"
    outside.mkdir(parents=True, exist_ok=True)
    return outside


def _make_deps(
    *,
    cfg: Config | None = None,
    issue_dict: dict | None = None,
    agent_result: object = None,
    github_svc: MagicMock | None = None,
) -> MagicMock:
    deps = MagicMock()
    deps.cfg = cfg or Config()
    deps.status_display = MagicMock()
    if github_svc is not None:
        deps.github_svc = github_svc
    else:
        deps.github_svc = MagicMock()
        if issue_dict is not None:
            deps.github_svc.get_issue.return_value = issue_dict
    deps.agent_runner = MagicMock()
    deps.agent_runner.run = AsyncMock(return_value=agent_result)
    return deps


def _make_prompt() -> object:
    return build_prompt_invocation(
        PromptTemplate.PREFLIGHT_ISSUE,
        build_preflight_scope_args(
            check_name="ruff",
            command="ruff check .",
            output="All good",
        ),
    )


def test_run_diagnostic_reporter_dispatch_accept_mount_validated_afk(tmp_path):
    mount_path = _make_valid_mount(tmp_path)
    issue_output = IssueOutput(number=42, labels=["bug", "behavior-slice"])
    deps = _make_deps(
        agent_result=issue_output,
        issue_dict={"body": "x" * 100, "labels": ["bug", "behavior-slice"]},
    )
    deps.repo_root = tmp_path

    result = asyncio.run(
        run_diagnostic_reporter_dispatch(
            caller="Host-Check Reporter",
            diagnostic_role=AgentRole.PREFLIGHT_ISSUE.value,
            role_name=AgentRole.PREFLIGHT_ISSUE.value,
            original_failure_summary="Host check 'ruff' failed.",
            prompt_invocation=_make_prompt(),
            stage_override=StageOverride(service="claude"),
            mount_path=mount_path,
            deps=deps,
        )
    )

    assert result == DiagnosticReporterDispatchAFK(issue_number=42)
    deps.agent_runner.run.assert_awaited_once()


def test_run_diagnostic_reporter_dispatch_accept_mount_validated_hitl(tmp_path):
    mount_path = _make_valid_mount(tmp_path)
    issue_output = IssueOutput(number=55, labels=["bug", "behavior-slice"])
    deps = _make_deps(
        agent_result=issue_output,
        issue_dict={"body": "x" * 100, "labels": ["bug", "ready-for-human"]},
    )
    deps.repo_root = tmp_path

    result = asyncio.run(
        run_diagnostic_reporter_dispatch(
            caller="Host-Check Reporter",
            diagnostic_role=AgentRole.PREFLIGHT_ISSUE.value,
            role_name=AgentRole.PREFLIGHT_ISSUE.value,
            original_failure_summary="Host check 'ruff' failed.",
            prompt_invocation=_make_prompt(),
            stage_override=StageOverride(service="claude"),
            mount_path=mount_path,
            deps=deps,
        )
    )

    assert result == DiagnosticReporterDispatchHITL(issue_number=55)
    deps.agent_runner.run.assert_awaited_once()


def test_run_diagnostic_reporter_dispatch_mount_hard_reject_returns_mount_fallback(
    tmp_path,
):
    mount_path = _make_rejected_mount(tmp_path)
    github_svc = MagicMock()
    github_svc.repo = "owner/repo"
    github_svc.search_open_issues_by_title.return_value = []
    github_svc.create_issue_in.return_value = (99, 10099)
    deps = _make_deps(github_svc=github_svc)
    deps.repo_root = tmp_path

    result = asyncio.run(
        run_diagnostic_reporter_dispatch(
            caller="Host-Check Reporter",
            diagnostic_role=AgentRole.PREFLIGHT_ISSUE.value,
            role_name=AgentRole.PREFLIGHT_ISSUE.value,
            original_failure_summary="Host check 'ruff' failed.",
            prompt_invocation=_make_prompt(),
            stage_override=StageOverride(service="claude"),
            mount_path=mount_path,
            deps=deps,
        )
    )

    assert result == DiagnosticReporterDispatchMountFallback(issue_number=99)
    deps.agent_runner.run.assert_not_awaited()


def test_run_diagnostic_reporter_dispatch_non_issue_output_raises_runtime_error(
    tmp_path,
):
    from pycastle.agents.output_protocol import CompletionOutput

    mount_path = _make_valid_mount(tmp_path)
    deps = _make_deps(agent_result=CompletionOutput())
    deps.repo_root = tmp_path

    with pytest.raises(RuntimeError, match="returned unexpected output type"):
        asyncio.run(
            run_diagnostic_reporter_dispatch(
                caller="Host-Check Reporter",
                diagnostic_role=AgentRole.PREFLIGHT_ISSUE.value,
                role_name=AgentRole.PREFLIGHT_ISSUE.value,
                original_failure_summary="Host check 'ruff' failed.",
                prompt_invocation=_make_prompt(),
                stage_override=StageOverride(service="claude"),
                mount_path=mount_path,
                deps=deps,
            )
        )


def test_run_diagnostic_reporter_dispatch_skip_validation_returns_validation_skipped(
    tmp_path,
):
    mount_path = _make_valid_mount(tmp_path)
    issue_output = IssueOutput(number=77, labels=["bug", "behavior-slice"])
    deps = _make_deps(agent_result=issue_output)
    deps.repo_root = tmp_path

    result = asyncio.run(
        run_diagnostic_reporter_dispatch(
            caller="Host-Check Reporter",
            diagnostic_role=AgentRole.PREFLIGHT_ISSUE.value,
            role_name=AgentRole.PREFLIGHT_ISSUE.value,
            original_failure_summary="Host check 'ruff' failed.",
            prompt_invocation=_make_prompt(),
            stage_override=StageOverride(service="claude"),
            mount_path=mount_path,
            deps=deps,
            skip_validation=True,
        )
    )

    assert result == DiagnosticReporterDispatchValidationSkipped(issue_number=77)
    deps.github_svc.get_issue.assert_not_called()


def test_run_diagnostic_reporter_dispatch_pre_run_hook_runs_before_agent(tmp_path):
    call_log: list[str] = []
    mount_path = _make_valid_mount(tmp_path)
    issue_output = IssueOutput(number=42, labels=["bug", "behavior-slice"])

    async def _recording_run(request):
        call_log.append("agent")
        return issue_output

    deps = _make_deps(
        issue_dict={"body": "x" * 100, "labels": ["bug", "behavior-slice"]},
    )
    deps.repo_root = tmp_path
    deps.agent_runner.run = _recording_run

    def pre_run_hook(path: Path, invocation: object) -> None:
        call_log.append("hook")

    result = asyncio.run(
        run_diagnostic_reporter_dispatch(
            caller="Host-Check Reporter",
            diagnostic_role=AgentRole.PREFLIGHT_ISSUE.value,
            role_name=AgentRole.PREFLIGHT_ISSUE.value,
            original_failure_summary="Host check 'ruff' failed.",
            prompt_invocation=_make_prompt(),
            stage_override=StageOverride(service="claude"),
            mount_path=mount_path,
            deps=deps,
            pre_run_hook=pre_run_hook,
        )
    )

    assert result == DiagnosticReporterDispatchAFK(issue_number=42)
    assert call_log == ["hook", "agent"]
