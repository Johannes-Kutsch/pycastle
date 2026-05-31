import asyncio
import dataclasses
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

from pycastle.agents.output_protocol import AgentRole, CompletionOutput, IssueOutput
from pycastle.config import Config, StageOverride
from pycastle.iteration import ImproveContinue
from pycastle.iteration.improve import improve_phase
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.services import GitService, GithubService, ServiceRegistry
from pycastle.services.codex_service import CodexService
from pycastle.session.provider_session_state import (
    save_service_session_id,
    save_service_session_metadata,
)
from tests.support import (
    FakeAgentRunner,
    RecordingLogger,
    RecordingStatusDisplay,
    _make_deps,
)


@dataclass(frozen=True)
class _FakeService:
    name: str
    relpath: str
    resumable: bool = True

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        return self.relpath

    def is_resumable(self, state_dir: Path) -> bool:
        return self.resumable


def _seed_clean_phase_2_entry(
    worktree: Path,
    *,
    service_name: str,
    provider_session_id: str = "thread-exact",
) -> None:
    role_dir = worktree / ".pycastle-session" / "improve"
    role_dir.mkdir(parents=True, exist_ok=True)
    (role_dir / "_phase_progress").write_text("01-scan:picked", encoding="utf-8")
    if service_name == "codex":
        rollout_dir = (
            role_dir / "main" / service_name / "sessions" / "2026" / "05" / "30"
        )
        rollout_dir.mkdir(parents=True, exist_ok=True)
        (rollout_dir / "rollout-001.jsonl").write_text(
            '{"type":"thread.started","thread_id":"thread-exact"}\n',
            encoding="utf-8",
        )
    else:
        state_dir = worktree / f"custom/{service_name}-state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "session_id").write_text(
            f"{provider_session_id}\n",
            encoding="utf-8",
        )
    save_service_session_id(role_dir / "main", service_name, provider_session_id)
    save_service_session_metadata(role_dir / "main", service_name, provider_session_id)


def _make_seeded_improve_deps(
    tmp_path: Path,
    *,
    runner: FakeAgentRunner,
    selected_service: str,
    registry: ServiceRegistry,
    recorded_service: str = "codex",
    seed_on_create: bool = True,
) -> tuple[Any, Path]:
    git_svc = MagicMock(spec=GitService)
    git_svc.get_head_sha.return_value = "abc123"
    git_svc.is_working_tree_clean.return_value = True
    git_svc.try_merge.return_value = True
    git_svc.is_ancestor.return_value = True
    git_svc.verify_ref_exists.return_value = False

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = []
    github_svc.get_issue.return_value = {"number": 42, "title": "PRD", "body": "body"}
    github_svc.get_issue_comments.return_value = []

    registered_worktrees: list[Path] = []
    sandbox_path = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"

    def _create_worktree(
        repo: Path, path: Path, branch: str, sha: str | None = None
    ) -> None:
        path.mkdir(parents=True, exist_ok=True)
        (path / "pyproject.toml").write_text("[project]\nname='t'\n", encoding="utf-8")
        if seed_on_create:
            _seed_clean_phase_2_entry(path, service_name=recorded_service)
        registered_worktrees.append(path)

    def _list_worktrees(repo: Path) -> list[Path]:
        return list(registered_worktrees)

    def _remove_worktree(repo: Path, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)
        registered_worktrees[:] = [
            item for item in registered_worktrees if item != path
        ]

    git_svc.create_worktree.side_effect = _create_worktree
    git_svc.list_worktrees.side_effect = _list_worktrees
    git_svc.remove_worktree.side_effect = _remove_worktree

    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=RecordingLogger(),
        status_display=RecordingStatusDisplay(),
        cfg=dataclasses.replace(
            Config(),
            improve_override=StageOverride(
                service=selected_service,
                model="model",
                effort="medium",
            ),
        ),
        service_registry=registry,
        setup_worktrees=False,
    )
    return deps, sandbox_path


def test_clean_phase_2_entry_dispatches_prd_prompt_for_exact_same_service_transcript(
    tmp_path: Path,
) -> None:
    runner = FakeAgentRunner(
        responses=[IssueOutput(number=42, labels=[]), CompletionOutput()]
    )
    deps, _ = _make_seeded_improve_deps(
        tmp_path,
        runner=runner,
        selected_service="codex",
        registry=ServiceRegistry({"codex": CodexService()}),
    )

    result = asyncio.run(improve_phase(deps))

    assert isinstance(result, ImproveContinue)
    assert [call.name for call in runner.calls] == ["PRD Agent", "Slice Agent"]
    assert runner.calls[0].template is PromptTemplate.IMPROVE_PRD
    assert runner.calls[0].send_role_prompt_on_resume is True
    assert runner.calls[0].session_namespace == "main"


def test_clean_phase_2_entry_rejects_missing_exact_transcript_and_restarts_from_phase_1(
    tmp_path: Path,
) -> None:
    runner = FakeAgentRunner(responses=[])
    deps, sandbox_path = _make_seeded_improve_deps(
        tmp_path,
        runner=runner,
        selected_service="codex",
        registry=ServiceRegistry({"codex": CodexService()}),
    )
    deps.git_svc.create_worktree.side_effect = lambda repo, path, branch, sha=None: (
        path.mkdir(parents=True, exist_ok=True),
        (path / "pyproject.toml").write_text("[project]\nname='t'\n", encoding="utf-8"),
        (path / ".pycastle-session" / "improve").mkdir(parents=True, exist_ok=True),
        (path / ".pycastle-session" / "improve" / "_phase_progress").write_text(
            "01-scan:picked", encoding="utf-8"
        ),
    )

    result = asyncio.run(improve_phase(deps))

    assert isinstance(result, ImproveContinue)
    assert runner.calls == []
    assert not (sandbox_path / ".pycastle-session" / "improve").exists()
    assert (
        "Restarting improve from phase 1 because the phase 1 transcript handoff "
        "is unavailable for a clean phase 2 entry."
        in [str(call[2]) for call in deps.status_display.calls if call[0] == "print"]
    )


def test_rejected_clean_phase_2_entry_returns_to_phase_1_on_next_improve_dispatch(
    tmp_path: Path,
) -> None:
    runner = FakeAgentRunner(
        responses=[
            CompletionOutput(),
            IssueOutput(number=42, labels=[]),
            CompletionOutput(),
        ]
    )
    deps, _ = _make_seeded_improve_deps(
        tmp_path,
        runner=runner,
        selected_service="codex",
        registry=ServiceRegistry({"codex": CodexService()}),
        seed_on_create=False,
    )
    create_calls = {"count": 0}

    def _create_worktree(
        repo: Path, path: Path, branch: str, sha: str | None = None
    ) -> None:
        path.mkdir(parents=True, exist_ok=True)
        (path / "pyproject.toml").write_text("[project]\nname='t'\n", encoding="utf-8")
        if create_calls["count"] == 0:
            role_dir = path / ".pycastle-session" / "improve"
            role_dir.mkdir(parents=True, exist_ok=True)
            (role_dir / "_phase_progress").write_text(
                "01-scan:picked", encoding="utf-8"
            )
        create_calls["count"] += 1

    deps.git_svc.create_worktree.side_effect = _create_worktree

    first_result = asyncio.run(improve_phase(deps))
    second_result = asyncio.run(improve_phase(deps))

    assert isinstance(first_result, ImproveContinue)
    assert isinstance(second_result, ImproveContinue)
    assert [call.name for call in runner.calls] == [
        "Scan Agent",
        "PRD Agent",
        "Slice Agent",
    ]
    assert runner.calls[0].template is PromptTemplate.IMPROVE_SCAN
    assert runner.calls[0].session_namespace == "main"


def test_cross_service_clean_phase_2_entry_restarts_instead_of_running_prd_on_other_provider_transcript(
    tmp_path: Path,
) -> None:
    runner = FakeAgentRunner(responses=[])
    deps, sandbox_path = _make_seeded_improve_deps(
        tmp_path,
        runner=runner,
        selected_service="opencode",
        registry=ServiceRegistry(
            {
                "codex": CodexService(),
                "opencode": cast(
                    Any,
                    _FakeService(
                        name="opencode",
                        relpath="custom/opencode-state/",
                    ),
                ),
            }
        ),
    )

    result = asyncio.run(improve_phase(deps))

    assert isinstance(result, ImproveContinue)
    assert runner.calls == []
    assert not (sandbox_path / ".pycastle-session" / "improve").exists()
