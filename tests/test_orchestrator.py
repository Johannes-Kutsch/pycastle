import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlparse
from unittest.mock import MagicMock, patch

import pytest

from pycastle.agents.output_protocol import (
    CompletionOutput,
    IssueOutput,
    PlannerOutput,
    PromiseParseError,
)
from pycastle.agents.runner import RunRequest
from pycastle.runtime_session import (
    ProviderSessionPreferences,
    ProviderSessionPreferencesRequest,
    ProviderSessionState,
    ProviderSessionStateRequest,
)
from pycastle.config import StageOverride
from pycastle.errors import (
    AgentTimeoutError,
    ModelNotAvailableError,
    SetupPhaseError,
    UsageLimitError,
)
from pycastle.infrastructure.preflight_failure_interpreter import (
    PreflightCommandFailure,
)
from pycastle.services import (
    GitCommandError,
    GithubAPIError,
    GithubAuthError,
    OperatorActionableGithubError,
    GithubService,
    GitService,
    ServiceRegistry,
)
from pycastle.services.runtime_services import AgentService
from tests.support import FakeAgentRunner, RecordingStatusDisplay, _make_deps
from pycastle.infrastructure.worktree import prune_orphan_worktrees
from pycastle.iteration.orchestrator import (
    ensure_session_excludes,
    run,
)
from pycastle.iteration import AbortedAgentCredentialFailure
from pycastle.session import RunKind


# ── helpers ───────────────────────────────────────────────────────────────────


def _plan_output(issues: list[dict]) -> PlannerOutput:
    return PlannerOutput(
        issues=[
            {
                "number": i["number"],
                "title": i["title"],
                "labels": i.get("labels", ["behavior-slice"]),
            }
            for i in issues
        ]
    )


def _preflight_failure(
    check_name: str, command: str, output: str
) -> PreflightCommandFailure:
    return PreflightCommandFailure(
        check_name=check_name,
        command=command,
        output=output,
    )


def _make_git_svc(try_merge_side_effect=None, is_ancestor=True):
    mock_svc = MagicMock(spec=GitService)
    mock_svc.get_head_sha.return_value = "abc1234"
    mock_svc.verify_ref_exists.return_value = False
    mock_svc.list_worktrees.return_value = []
    if try_merge_side_effect is not None:
        results = list(try_merge_side_effect)
        idx = [0]

        def _try_merge(repo_path, branch):
            val = results[idx[0]]
            idx[0] += 1
            return val

        mock_svc.try_merge.side_effect = _try_merge
    else:
        mock_svc.try_merge.return_value = True
    mock_svc.is_ancestor.return_value = is_ancestor
    mock_svc.start_merge.return_value = False

    def _fake_create_worktree(repo, wt, branch, sha=None):
        wt.mkdir(parents=True, exist_ok=True)
        (wt / "pyproject.toml").write_text("[project]\nname='t'\n")

    def _fake_remove_worktree(repo, wt):
        import shutil

        if isinstance(wt, Path) and wt.exists():
            shutil.rmtree(wt)

    mock_svc.create_worktree.side_effect = _fake_create_worktree
    mock_svc.remove_worktree.side_effect = _fake_remove_worktree
    return mock_svc


def _make_github_svc(numbers: list[int] | None = None):
    mock = MagicMock(spec=GithubService)
    if numbers is None:
        issues = [
            {
                "number": 1,
                "title": "Default Issue",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            },
            {
                "number": 2,
                "title": "Default Issue 2",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            },
        ]
    else:
        issues = [
            {
                "number": n,
                "title": f"Issue {n}",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            }
            for n in numbers
        ]
    mock.get_open_issues.return_value = issues
    mock.get_all_open_issues_lightweight.return_value = []
    mock.repo = "test/repo"
    mock.search_open_issues_by_title.return_value = []
    mock.create_issue_in.return_value = 999
    return mock


def _make_github_svc_afk():
    """GithubService mock for AFK path (verdict comes from agent output label)."""
    mock = MagicMock(spec=GithubService)
    mock.get_issue_title.return_value = "Preflight fix title"
    mock.get_open_issues.return_value = [
        {"number": 1, "title": "Default Issue", "body": "x" * 100, "comments": []}
    ]
    mock.get_all_open_issues_lightweight.return_value = []
    return mock


def _make_github_svc_hitl():
    """GithubService mock for HITL path (verdict comes from agent output label)."""
    mock = MagicMock(spec=GithubService)
    mock.get_issue_title.return_value = "Preflight fix title"
    mock.get_open_issues.return_value = [
        {"number": 1, "title": "Default Issue", "body": "x" * 100, "comments": []}
    ]
    mock.get_all_open_issues_lightweight.return_value = []
    return mock


class _FakeService:
    """Minimal AgentService stub for orchestrator failover tests."""

    def __init__(
        self,
        available: bool = True,
        wake_time=None,
        names: list[str] | None = None,
    ) -> None:
        self._available = available
        self._wake_time = wake_time
        self._names = names or []

    def is_available(self, now=None, *, model=None) -> bool:
        return self._available

    def next_wake_time(self):
        return self._wake_time

    def mark_exhausted(self, reset_time) -> None:
        pass

    def account_names(self) -> list[str]:
        return self._names

    # unused protocol stubs
    @property
    def name(self) -> str:
        return "fake"

    def build_command(self, role, model, effort, run_kind, session_uuid):
        return ""

    def build_env(self, state_dir_container_path=None, token=None):
        return {}

    def run(self, lines):
        return iter([])

    def state_dir_relpath(self, role, namespace=""):
        del role, namespace
        return None

    def is_resumable(self, state_dir):
        del state_dir
        return False

    def provider_session_preferences(
        self, request: ProviderSessionPreferencesRequest
    ) -> ProviderSessionPreferences:
        del request
        return ProviderSessionPreferences()

    def provider_session_state(
        self, request: ProviderSessionStateRequest
    ) -> ProviderSessionState:
        del request
        return ProviderSessionState(RunKind.FRESH, None)

    def valid_efforts(self) -> frozenset[str]:
        return frozenset({"medium"})

    def valid_models(self) -> frozenset[str]:
        return frozenset({"fake"})


class _SequencedAvailabilityService(_FakeService):
    def __init__(
        self,
        availability: list[bool],
        *,
        wake_time=None,
        names: list[str] | None = None,
    ) -> None:
        super().__init__(
            available=availability[-1] if availability else True,
            wake_time=wake_time,
            names=names,
        )
        self._availability = list(availability)

    def is_available(self, now=None, *, model=None) -> bool:
        if self._availability:
            self._available = self._availability.pop(0)
        return self._available


class _RecordingServiceRegistry(ServiceRegistry):
    def __init__(self, services):
        super().__init__(services)
        self.resolve_calls: list[tuple[str, str, str]] = []

    def resolve(self, override: StageOverride, now: datetime) -> StageOverride:
        self.resolve_calls.append((override.service, override.model, override.effort))
        return super().resolve(override, now)


def _write_config(tmp_path: Path, **kwargs) -> None:
    (tmp_path / "pycastle").mkdir(exist_ok=True)
    lines = ["from pycastle import StageOverride", "from pathlib import Path"]
    for k, v in kwargs.items():
        if isinstance(v, StageOverride):
            lines.append(f"{k} = {v!r}")
        elif isinstance(v, Path):
            lines.append(f"{k} = Path({str(v)!r})")
        else:
            lines.append(f"{k} = {v!r}")
    (tmp_path / "pycastle" / "config.py").write_text("\n".join(lines) + "\n")


def _run(
    tmp_path,
    run_agent_fn=None,
    *,
    git_service=None,
    github_service=None,
    agent_runner=None,
    status_display=None,
    service_registry=None,
    improve_mode=None,
    **config_kwargs,
):
    config_kwargs.setdefault("max_parallel", 4)
    config_kwargs.setdefault("max_iterations", 1)
    _write_config(tmp_path, **config_kwargs)
    if run_agent_fn is not None and agent_runner is None:
        agent_runner = FakeAgentRunner(side_effect=run_agent_fn)
    asyncio.run(
        run(
            {},
            tmp_path,
            agent_runner=agent_runner,
            git_service=git_service if git_service is not None else _make_git_svc(),
            github_service=github_service,
            status_display=status_display,
            service_registry=service_registry,
            improve_mode=improve_mode,
        )
    )


# ── Issue 193: run() works when planner omits branch field ───────────────────


def test_run_does_not_crash_when_planner_omits_branch_field(tmp_path):
    """run() must not KeyError when planner output has no 'branch' key in issues."""
    dispatched: list[str] = []

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return PlannerOutput(
                issues=[
                    {
                        "number": 193,
                        "title": "Fix branch bug",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    }
                ]
            )
        if "Implement Agent" in request.name:
            dispatched.append((request.prompt.scope_args or {}).get("BRANCH", ""))
            return CompletionOutput()
        return CompletionOutput()

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        github_service=_make_github_svc(numbers=[193]),
    )

    assert dispatched == ["pycastle/issue-193"]


# ── Issue 188: deterministic branch names ────────────────────────────────────


def test_run_computes_branch_from_issue_number_not_planner_slug(tmp_path):
    """After parse_plan, each issue branch must be pycastle/issue-N, ignoring planner slug."""
    captured_branches: list[str] = []

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return PlannerOutput(
                issues=[
                    {
                        "number": 42,
                        "title": "Fix thing",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    }
                ]
            )
        if "Implement Agent" in request.name:
            captured_branches.append(
                (request.prompt.scope_args or {}).get("BRANCH", "")
            )
            return CompletionOutput()
        return CompletionOutput()

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        github_service=_make_github_svc(numbers=[42]),
    )

    assert captured_branches == ["pycastle/issue-42"], (
        f"Expected branch pycastle/issue-42; got {captured_branches}"
    )


# ── Cycle 24-B1: prune_orphan_worktrees deletes orphan dirs ──────────────────


def _make_git_service_for_prune(active_paths: list[Path]) -> GitService:
    mock_svc = MagicMock(spec=GitService)
    mock_svc.list_worktrees.return_value = active_paths
    return mock_svc


def test_prune_orphan_worktrees_deletes_absent_dir(tmp_path):
    worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)
    orphan = worktrees_dir / "orphan-branch"
    orphan.mkdir()

    prune_orphan_worktrees(tmp_path, git_service=_make_git_service_for_prune([]))

    assert not orphan.exists()


def test_prune_orphan_worktrees_deletes_only_orphans(tmp_path):
    """Only dirs absent from git worktree list must be deleted; active ones survive."""
    worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)
    orphan = worktrees_dir / "orphan"
    orphan.mkdir()
    active = worktrees_dir / "active-branch"
    active.mkdir()

    prune_orphan_worktrees(tmp_path, git_service=_make_git_service_for_prune([active]))

    assert not orphan.exists()
    assert active.exists()


# ── Cycle 24-B2: prune_orphan_worktrees preserves active worktrees ───────────


def test_prune_orphan_worktrees_preserves_active_dir(tmp_path):
    worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)
    active = worktrees_dir / "my-branch"
    active.mkdir()

    prune_orphan_worktrees(tmp_path, git_service=_make_git_service_for_prune([active]))

    assert active.exists()


def test_prune_orphan_worktrees_noop_when_dir_missing(tmp_path):
    """Must not raise if pycastle/.worktrees/ does not exist yet."""
    prune_orphan_worktrees(tmp_path)  # no exception — no git_service needed


# ── Issue 298: delete .worktrees dir when it becomes empty ───────────────────


def test_prune_orphan_worktrees_removes_parent_when_empty(tmp_path):
    worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)
    orphan = worktrees_dir / "orphan-branch"
    orphan.mkdir()

    prune_orphan_worktrees(tmp_path, git_service=_make_git_service_for_prune([]))

    assert not worktrees_dir.exists()


def test_prune_orphan_worktrees_keeps_parent_when_active_children_remain(tmp_path):
    worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)
    orphan = worktrees_dir / "orphan"
    orphan.mkdir()
    active = worktrees_dir / "active-branch"
    active.mkdir()

    prune_orphan_worktrees(tmp_path, git_service=_make_git_service_for_prune([active]))

    assert worktrees_dir.exists()


def test_prune_orphan_worktrees_removes_parent_when_already_empty(tmp_path):
    worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)

    prune_orphan_worktrees(tmp_path, git_service=_make_git_service_for_prune([]))

    assert not worktrees_dir.exists()


def test_prune_orphan_worktrees_keeps_parent_when_non_dir_file_remains(tmp_path):
    """A file (not directory) left in the worktrees dir blocks parent removal."""
    worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)
    orphan = worktrees_dir / "orphan-branch"
    orphan.mkdir()
    leftover_file = worktrees_dir / "stale.lock"
    leftover_file.write_text("lock")

    prune_orphan_worktrees(tmp_path, git_service=_make_git_service_for_prune([]))

    assert not orphan.exists()
    assert leftover_file.exists()
    assert worktrees_dir.exists()


# ── Cycle 24-C1/C2: error logging on agent failure ───────────────────────────


def test_failed_agent_appends_traceback_to_errors_log(tmp_path):
    logs_dir = tmp_path / "pycastle" / "logs"
    logs_dir.mkdir(parents=True)
    errors_log = logs_dir / "errors.log"

    boom = RuntimeError("something went wrong")

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix thing", "body": "x" * 100, "comments": []}]
            )
        raise boom

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        logs_dir=logs_dir,
    )

    content = errors_log.read_text()
    assert "RuntimeError" in content
    assert "something went wrong" in content


def test_failed_agent_errors_log_has_timestamp_separator(tmp_path):
    logs_dir = tmp_path / "pycastle" / "logs"
    logs_dir.mkdir(parents=True)
    errors_log = logs_dir / "errors.log"

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix thing", "body": "x" * 100, "comments": []}]
            )
        raise RuntimeError("boom")

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        logs_dir=logs_dir,
    )

    assert "---" in errors_log.read_text()


def test_failed_agent_prints_traceback_to_stderr(tmp_path, capsys):
    logs_dir = tmp_path / "pycastle" / "logs"
    logs_dir.mkdir(parents=True)

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix thing", "body": "x" * 100, "comments": []}]
            )
        raise RuntimeError("stderr traceback check")

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        logs_dir=logs_dir,
    )

    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert "stderr traceback check" in err


# ── Issue-78: model/effort passed per stage ───────────────────────────────────


def test_planner_receives_plan_stage_model_and_effort(tmp_path):
    """Planner run_agent call must include model and effort from plan stage override."""
    captured: list[dict] = []

    async def _fake_run_agent(request: RunRequest):
        captured.append(
            {"name": request.name, "model": request.model, "effort": request.effort}
        )
        return _plan_output([])

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        plan_override=StageOverride(model="claude-haiku-4-5", effort="low"),
    )

    planner_call = next(c for c in captured if c["name"] == "Plan Agent")
    assert planner_call["model"] == "claude-haiku-4-5"
    assert planner_call["effort"] == "low"


def test_implementer_receives_implement_stage_model_and_effort(tmp_path):
    """Each Implementer run_agent call must include model and effort from implement stage."""
    captured: list[dict] = []

    async def _fake_run_agent(request: RunRequest):
        captured.append(
            {"name": request.name, "model": request.model, "effort": request.effort}
        )
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return _plan_output(
            [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
        )

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(),
        github_service=_make_github_svc(),
        implement_override=StageOverride(model="claude-sonnet-4-6", effort="high"),
    )

    impl_call = next(c for c in captured if "Implement Agent" in c["name"])
    assert impl_call["model"] == "claude-sonnet-4-6"
    assert impl_call["effort"] == "high"


def test_reviewer_receives_review_stage_model_and_effort(tmp_path):
    """Each Reviewer run_agent call must include model and effort from review stage."""
    captured: list[dict] = []

    async def _fake_run_agent(request: RunRequest):
        captured.append(
            {"name": request.name, "model": request.model, "effort": request.effort}
        )
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return _plan_output(
            [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
        )

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(),
        github_service=_make_github_svc(),
        review_override=StageOverride(model="claude-haiku-4-5", effort="medium"),
    )

    rev_call = next(c for c in captured if "Review Agent" in c["name"])
    assert rev_call["model"] == "claude-haiku-4-5"
    assert rev_call["effort"] == "medium"


def test_merger_receives_merge_stage_model_and_effort(tmp_path):
    """Merger run_agent call must include model and effort from merge stage override."""
    captured: list[dict] = []

    async def _fake_run_agent(request: RunRequest):
        captured.append(
            {"name": request.name, "model": request.model, "effort": request.effort}
        )
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return _plan_output(
            [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
        )

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[False]),
        github_service=_make_github_svc(),
        merge_override=StageOverride(model="claude-opus-4-7", effort="low"),
    )

    merger_call = next(c for c in captured if c["name"] == "Merge Agent")
    assert merger_call["model"] == "claude-opus-4-7"
    assert merger_call["effort"] == "low"


def test_cross_service_config_threads_service_to_core_phase_requests(tmp_path):
    """Implementer, Reviewer, and Merger must each receive their stage override service."""
    captured: list[dict] = []

    async def _fake_run_agent(request: RunRequest):
        captured.append({"name": request.name, "service": request.service})
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
            )
        return CompletionOutput()

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[False]),
        github_service=_make_github_svc(),
        implement_override=StageOverride(
            service="codex", model="gpt-5.3-codex-spark", effort="medium"
        ),
        review_override=StageOverride(
            service="claude", model="sonnet", effort="medium"
        ),
        merge_override=StageOverride(service="codex", model="gpt-5.5", effort="medium"),
    )

    impl_call = next(c for c in captured if "Implement Agent" in c["name"])
    rev_call = next(c for c in captured if "Review Agent" in c["name"])
    merger_call = next(c for c in captured if c["name"] == "Merge Agent")
    assert impl_call["service"] == "codex"
    assert rev_call["service"] == "claude"
    assert merger_call["service"] == "codex"


def test_default_planner_stage_override_passes_configured_model_and_effort(tmp_path):
    """The Planner stage must receive the bundled default model and effort."""
    captured: list[dict] = []

    async def _fake_run_agent(request: RunRequest):
        captured.append(
            {"name": request.name, "model": request.model, "effort": request.effort}
        )
        return _plan_output([])

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
    )

    planner_call = next(c for c in captured if c["name"] == "Plan Agent")
    assert planner_call["model"] == "kimi-k2.6"
    assert planner_call["effort"] == "medium"


def test_stage_overrides_are_independent(tmp_path):
    """Different stages must receive their own independent model/effort values."""
    captured: list[dict] = []

    async def _fake_run_agent(request: RunRequest):
        captured.append(
            {"name": request.name, "model": request.model, "effort": request.effort}
        )
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return _plan_output(
            [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
        )

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[False]),
        github_service=_make_github_svc(),
        plan_override=StageOverride(model="claude-haiku-4-5", effort="low"),
        implement_override=StageOverride(model="claude-sonnet-4-6", effort="medium"),
        review_override=StageOverride(model="claude-haiku-4-5", effort=""),
        merge_override=StageOverride(model="claude-opus-4-7", effort="high"),
    )

    by_name = {c["name"]: c for c in captured}
    assert by_name["Plan Agent"]["model"] == "claude-haiku-4-5"
    assert by_name["Plan Agent"]["effort"] == "low"
    assert by_name["Implement Agent #1"]["model"] == "claude-sonnet-4-6"
    assert by_name["Implement Agent #1"]["effort"] == "medium"
    assert by_name["Review Agent #1"]["model"] == "claude-haiku-4-5"
    assert by_name["Review Agent #1"]["effort"] == ""

    assert by_name["Merge Agent"]["model"] == "claude-opus-4-7"
    assert by_name["Merge Agent"]["effort"] == "high"


# ── Issue-100: stage parameter ───────────────────────────────────────────────


def test_each_agent_passes_correct_stage_string(tmp_path):
    """Planner, Implementer, Reviewer, and Merger must each pass the correct stage= string."""
    captured: list[dict] = []

    async def _fake_run_agent(request: RunRequest):
        captured.append({"name": request.name, "stage": request.stage})
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return _plan_output(
            [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
        )

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[False]),
        github_service=_make_github_svc(),
    )

    by_name = {c["name"]: c for c in captured}
    assert by_name["Plan Agent"]["stage"] == "plan-sandbox"
    assert by_name["Implement Agent #1"]["stage"] == "pre-implementation"
    assert by_name["Review Agent #1"]["stage"] == "pre-review"
    assert by_name["Merge Agent"]["stage"] == "pre-merge"


# ── Issue-95: parallel implementers with bounded concurrency ──────────────────


def test_multiple_implementers_run_in_parallel(tmp_path):
    """With MAX_PARALLEL >= N issues, all N implementers must be active simultaneously."""
    active_implementers: set[str] = set()
    max_concurrent = 0

    async def _fake_run_agent(request: RunRequest):
        nonlocal max_concurrent
        if request.name == "Plan Agent":
            return _plan_output(
                [
                    {"number": i, "title": f"Issue {i}", "branch": f"issue/{i}"}
                    for i in range(1, 4)
                ]
            )
        if "Implement Agent" in request.name:
            active_implementers.add(request.name)
            max_concurrent = max(max_concurrent, len(active_implementers))
            await asyncio.sleep(0.05)
            active_implementers.discard(request.name)
            return CompletionOutput()
        return CompletionOutput()

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(),
        github_service=_make_github_svc(numbers=[1, 2, 3]),
    )

    assert max_concurrent == 3, (
        f"Expected all 3 implementers active simultaneously, max was {max_concurrent}"
    )


def test_concurrent_agents_never_exceed_max_parallel(tmp_path):
    """The total number of concurrently active agents must never exceed MAX_PARALLEL."""
    active_count = 0
    max_active = 0
    max_parallel = 3

    async def _fake_run_agent(request: RunRequest):
        nonlocal active_count, max_active
        if request.name == "Plan Agent":
            return _plan_output(
                [
                    {"number": i, "title": f"Issue {i}", "branch": f"issue/{i}"}
                    for i in range(1, 8)
                ]
            )
        active_count += 1
        max_active = max(max_active, active_count)
        await asyncio.sleep(0.01)
        active_count -= 1
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return CompletionOutput()

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(),
        github_service=_make_github_svc(numbers=list(range(1, 8))),
        max_parallel=max_parallel,
    )

    assert max_active <= max_parallel, (
        f"Active agents exceeded MAX_PARALLEL={max_parallel}: max observed={max_active}"
    )


def test_implementer_starts_while_reviewer_runs(tmp_path):
    """A new Implementer must be able to start while a prior issue's Reviewer is running."""
    events: list[str] = []

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [
                    {"number": i, "title": f"Issue {i}", "branch": f"issue/{i}"}
                    for i in range(1, 4)
                ]
            )
        events.append(f"start:{request.name}")
        await asyncio.sleep(0.03)
        events.append(f"end:{request.name}")
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return CompletionOutput()

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(),
        github_service=_make_github_svc(numbers=[1, 2, 3]),
        max_parallel=3,
    )

    impl_3_start = next(
        (i for i, e in enumerate(events) if e == "start:Implement Agent #3"), None
    )
    rev_1_end = next(
        (i for i, e in enumerate(events) if e == "end:Review Agent #1"), None
    )

    assert impl_3_start is not None, "Implement Agent #3 must start"
    assert rev_1_end is not None, "Review Agent #1 must finish"
    assert impl_3_start < rev_1_end, (
        f"Implement Agent #3 must start before Review Agent #1 finishes; events={events}"
    )


# ── Issue-101: sequential merge loop with post-merge checks ──────────────────


def test_clean_merges_skip_merger(tmp_path):
    """When all branches merge cleanly, Merger agent must NOT be spawned."""
    agent_names: list[str] = []

    issues = [
        {
            "number": 1,
            "title": "Fix A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Fix B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]

    async def _fake_run_agent(request: RunRequest):
        agent_names.append(request.name)
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return _plan_output(issues)

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True, True]),
        github_service=_make_github_svc(),
    )

    assert "Merge Agent" not in agent_names, (
        f"Merge Agent must not be spawned on clean merges; agents called: {agent_names}"
    )


def test_clean_merge_calls_close_issue_with_parents_per_issue(
    tmp_path,
):
    """Each cleanly-merged issue must be closed via close_issue_with_parents()."""
    issues = [
        {
            "number": 7,
            "title": "Fix A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 8,
            "title": "Fix B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]

    async def _fake_run_agent(request: RunRequest):
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return _plan_output(issues)

    mock_github = _make_github_svc(numbers=[7, 8])
    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True, True]),
        github_service=mock_github,
    )

    closed = [
        call.args[0] for call in mock_github.close_issue_with_parents.call_args_list
    ]
    assert sorted(closed) == [7, 8], f"Expected issues 7 and 8 closed; got {closed}"
    assert not hasattr(mock_github, "close_completed_parent_issues")


def test_conflict_branch_spawns_merger_with_only_failing_branch(tmp_path):
    """When one branch conflicts, Merger is spawned with only the conflicting branch."""
    captured: list[dict] = []

    issues = [
        {
            "number": 1,
            "title": "Clean",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Conflict",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]

    async def _fake_run_agent(request: RunRequest):
        captured.append(
            {"name": request.name, "scope_args": (request.prompt.scope_args or {})}
        )
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return _plan_output(issues)

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True, False]),
        github_service=_make_github_svc(),
    )

    merger_calls = [c for c in captured if c["name"] == "Merge Agent"]
    assert len(merger_calls) == 1, (
        f"Expected exactly one Merger call; got {merger_calls}"
    )
    branches_arg = merger_calls[0]["scope_args"]["BRANCHES"]
    assert "pycastle/issue-2" in branches_arg
    assert "pycastle/issue-1" not in branches_arg


def test_conflict_branch_closed_after_merger_agent(tmp_path):
    """Conflicting branches must be closed by the orchestrator after the Merger agent returns."""
    issues = [
        {
            "number": 1,
            "title": "Clean",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Conflict",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]

    async def _fake_run_agent(request: RunRequest):
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return _plan_output(issues)

    mock_github = _make_github_svc()
    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True, False]),
        github_service=mock_github,
    )

    closed = [
        call.args[0] for call in mock_github.close_issue_with_parents.call_args_list
    ]
    assert 2 in closed, (
        f"Conflict issue #2 must be closed after Merger; closed: {closed}"
    )
    assert 1 in closed, f"Clean issue #1 must also be closed; closed: {closed}"


def test_conflict_merge_does_not_call_close_completed_parent_issues(tmp_path):
    """Conflict merge relies on per-issue cascade and skips the global parent scan."""
    issues = [{"number": 5, "title": "Conflict", "body": "x" * 100, "comments": []}]

    async def _fake_run_agent(request: RunRequest):
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return _plan_output(issues)

    mock_github = _make_github_svc(numbers=[5])
    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[False]),
        github_service=mock_github,
    )

    assert not hasattr(mock_github, "close_completed_parent_issues")


def test_merger_does_not_receive_issues_prompt_arg(tmp_path):
    """Merger must not receive an ISSUES prompt arg — issue closing is the orchestrator's job."""
    captured: list[dict] = []

    issues = [
        {
            "number": 3,
            "title": "Clean issue",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 4,
            "title": "Conflict issue",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]

    async def _fake_run_agent(request: RunRequest):
        captured.append(
            {"name": request.name, "scope_args": (request.prompt.scope_args or {})}
        )
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return _plan_output(issues)

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True, False]),
        github_service=_make_github_svc(numbers=[3, 4]),
    )

    merger_calls = [c for c in captured if c["name"] == "Merge Agent"]
    assert len(merger_calls) == 1
    assert "ISSUES" not in merger_calls[0]["scope_args"], (
        "Merger must not receive an ISSUES prompt arg"
    )


def test_multiple_conflict_issues_all_closed_after_merger(tmp_path):
    """Each conflict issue must be individually closed when there are multiple conflicts."""
    issues = [
        {
            "number": 10,
            "title": "Conflict A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 11,
            "title": "Conflict B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 12,
            "title": "Conflict C",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]

    async def _fake_run_agent(request: RunRequest):
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return _plan_output(issues)

    mock_github = _make_github_svc(numbers=[10, 11, 12])
    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[False, False, False]),
        github_service=mock_github,
    )

    closed = [
        call.args[0] for call in mock_github.close_issue_with_parents.call_args_list
    ]
    assert 10 in closed, f"Conflict issue #10 must be closed; closed: {closed}"
    assert 11 in closed, f"Conflict issue #11 must be closed; closed: {closed}"
    assert 12 in closed, f"Conflict issue #12 must be closed; closed: {closed}"
    assert not hasattr(mock_github, "close_completed_parent_issues")


def test_preflight_issue_receives_correct_command_and_output(tmp_path):
    """preflight-issue agent must receive exact COMMAND and OUTPUT from the failing check."""
    captured: list[dict] = []

    async def _fake_run_agent(request: RunRequest):
        captured.append(
            {"name": request.name, "scope_args": (request.prompt.scope_args or {})}
        )
        if "Pre-Flight Reporter" in request.name:
            return IssueOutput(number=70, labels=["ready-for-human"])
        return CompletionOutput()

    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            agent_runner=FakeAgentRunner(
                side_effect=_fake_run_agent,
                preflight_responses=[
                    [
                        _preflight_failure(
                            "pytest",
                            "pytest -x",
                            "FAILED tests/test_bar.py::test_something",
                        )
                    ]
                ],
            ),
            github_service=_make_github_svc_hitl(),
        )

    pf_calls = [c for c in captured if "Pre-Flight Reporter" in c["name"]]
    assert len(pf_calls) == 1
    args = pf_calls[0]["scope_args"]
    assert args.get("COMMAND") == "pytest -x", (
        f"COMMAND must be 'pytest -x'; got {args.get('COMMAND')!r}"
    )
    assert args.get("OUTPUT") == "FAILED tests/test_bar.py::test_something", (
        f"OUTPUT mismatch; got {args.get('OUTPUT')!r}"
    )


def test_clean_merged_branches_are_deleted_after_try_merge(tmp_path):
    async def _fake_run_agent(request: RunRequest):
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return _plan_output(
            [{"number": 1, "title": "Fix A", "body": "x" * 100, "comments": []}]
        )

    mock_git = _make_git_svc(try_merge_side_effect=[True], is_ancestor=True)
    _run(
        tmp_path,
        _fake_run_agent,
        git_service=mock_git,
        github_service=_make_github_svc(),
    )

    mock_git.delete_branch.assert_called_with("pycastle/issue-1", tmp_path)


def test_conflict_branches_are_deleted_after_merger_agent(tmp_path):
    """Branches resolved by the Merger agent must be deleted after it returns."""

    async def _fake_run_agent(request: RunRequest):
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return _plan_output(
            [{"number": 2, "title": "Conflict", "body": "x" * 100, "comments": []}]
        )

    mock_git = _make_git_svc(try_merge_side_effect=[False], is_ancestor=True)
    _run(
        tmp_path,
        _fake_run_agent,
        git_service=mock_git,
        github_service=_make_github_svc(),
    )

    mock_git.delete_branch.assert_called_with("pycastle/issue-2", tmp_path)


def test_non_ancestor_branch_not_deleted(tmp_path):
    """A branch that is not an ancestor of HEAD must not be deleted."""

    async def _fake_run_agent(request: RunRequest):
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return _plan_output(
            [{"number": 1, "title": "Fix A", "body": "x" * 100, "comments": []}]
        )

    mock_git = _make_git_svc(try_merge_side_effect=[True], is_ancestor=False)
    _run(
        tmp_path,
        _fake_run_agent,
        git_service=mock_git,
        github_service=_make_github_svc(),
    )

    mock_git.delete_branch.assert_not_called()


def test_delete_branch_error_does_not_abort_run(tmp_path):
    """A GitCommandError on delete_branch must not propagate out of run()."""

    async def _fake_run_agent(request: RunRequest):
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return _plan_output(
            [{"number": 1, "title": "Fix A", "body": "x" * 100, "comments": []}]
        )

    mock_git = _make_git_svc(try_merge_side_effect=[True], is_ancestor=True)
    mock_git.delete_branch.side_effect = GitCommandError(
        "fail", returncode=1, stderr=""
    )
    _run(
        tmp_path,
        _fake_run_agent,
        git_service=mock_git,
        github_service=_make_github_svc(),
    )  # must not raise


def test_run_incomplete_implementers_skip_merge(tmp_path):
    """When no implementer produces COMPLETE, try_merge must never be called."""

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
            )
        raise PromiseParseError(
            "no COMPLETE tag"
        )  # implementer ran but didn't complete

    mock_git = _make_git_svc()
    _run(
        tmp_path,
        _fake_run_agent,
        git_service=mock_git,
        github_service=_make_github_svc(),
    )

    mock_git.try_merge.assert_not_called()


# ── error log directory creation ─────────────────────────────────────────────


def test_failed_agent_creates_logs_dir_if_missing(tmp_path):
    """run() must create logs_dir with parents if it does not exist before writing errors.log."""
    logs_dir = tmp_path / "new" / "nested" / "logs"

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
            )
        raise RuntimeError("agent failed")

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        logs_dir=logs_dir,
    )

    assert (logs_dir / "errors.log").exists()


def test_failed_agent_uses_effective_global_logs_dir_for_errors_log(
    tmp_path, monkeypatch
):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / "config.py").write_text(
        "from pathlib import Path\nlogs_dir = Path('shared-logs')\n"
    )
    monkeypatch.setenv("PYCASTLE_HOME", str(global_dir))
    project_dir = tmp_path / "My Project"
    project_dir.mkdir()

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
            )
        raise RuntimeError("agent failed")

    asyncio.run(
        run(
            {},
            project_dir,
            agent_runner=FakeAgentRunner(side_effect=_fake_run_agent),
            git_service=_make_git_svc(),
            github_service=_make_github_svc(),
        )
    )

    assert (project_dir / "shared-logs" / "my-project" / "errors.log").exists()


def test_failed_agent_with_service_registry_uses_effective_global_logs_dir_for_errors_log(
    tmp_path, monkeypatch
):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / "config.py").write_text(
        "from pathlib import Path\nlogs_dir = Path('shared-logs')\n"
    )
    monkeypatch.setenv("PYCASTLE_HOME", str(global_dir))
    project_dir = tmp_path / "My Project"
    project_dir.mkdir()

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
            )
        raise RuntimeError("agent failed")

    asyncio.run(
        run(
            {},
            project_dir,
            agent_runner=FakeAgentRunner(side_effect=_fake_run_agent),
            git_service=_make_git_svc(),
            github_service=_make_github_svc(),
            service_registry=ServiceRegistry({"claude": _FakeService()}),
        )
    )

    assert (project_dir / "shared-logs" / "my-project" / "errors.log").exists()


# ── Issue-175: safe SHA pinning and skip-preflight logic ──────────────────────


def test_safe_sha_pinned_and_passed_to_implementer_after_preplanning_preflight(
    tmp_path,
):
    """PreflightCache uses the safe HEAD SHA for the preflight-sandbox.
    SHA threading to implement_phase is handled in slice 2; in slice 1
    implement_phase receives sha=None (transitional)."""
    fake_sha = "deadbeef123"

    mock_git = _make_git_svc(try_merge_side_effect=[True])
    mock_git.get_head_sha.return_value = fake_sha

    async def _fake_run_agent(request: RunRequest):
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return _plan_output(
            [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
        )

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=mock_git,
        github_service=_make_github_svc(),
    )

    # The preflight-sandbox is created via checkout_detached (not create_worktree).
    # SHA threading to implement worktrees is slice 2; implementer gets sha=None here.
    detached_shas = {
        c.args[2] for c in mock_git.checkout_detached.call_args_list if len(c.args) > 2
    }
    assert fake_sha in detached_shas, (
        f"PreflightCache must call checkout_detached with {fake_sha!r}; got {detached_shas}"
    )


def test_preplanning_preflight_runs_on_cold_startup(tmp_path):
    """On cold startup the Planner must not be called when get_open_issues returns empty."""
    planner_calls: list[str] = []

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            planner_calls.append(request.name)
            return _plan_output([])
        return CompletionOutput()

    github_svc = _make_github_svc()
    github_svc.get_open_issues.return_value = []
    _run(tmp_path, _fake_run_agent, github_service=github_svc)

    assert len(planner_calls) == 0, (
        f"Expected 0 Planner calls; got {len(planner_calls)}"
    )


def test_pinned_sha_is_passed_to_each_implementer(tmp_path):
    """PreflightCache uses the safe HEAD SHA for the preflight-sandbox.
    SHA threading to each implementer worktree is handled in slice 2; in slice 1
    all implementers receive sha=None (transitional)."""
    fake_sha = "cafebabe000"

    mock_git = _make_git_svc(try_merge_side_effect=[True, True])
    mock_git.get_head_sha.return_value = fake_sha

    issues = [
        {
            "number": 1,
            "title": "Fix A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Fix B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]

    async def _fake_run_agent(request: RunRequest):
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return _plan_output(issues)

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=mock_git,
        github_service=_make_github_svc(),
    )

    # The preflight-sandbox uses checkout_detached with the safe SHA.
    # SHA threading to implementer worktrees is slice 2; implementers get sha=None here.
    detached_shas = {
        c.args[2] for c in mock_git.checkout_detached.call_args_list if len(c.args) > 2
    }
    assert fake_sha in detached_shas, (
        f"PreflightCache must call checkout_detached with {fake_sha!r}; got {detached_shas}"
    )


# ── Issue-176: preflight failure handling and HITL routing ────────────────────


def test_preflight_failure_hitl_exits_nonzero_no_implementer(tmp_path):
    """On plan-sandbox preflight failure with HITL verdict, process must exit non-zero
    and no Implementer must be spawned."""
    implementer_calls: list[str] = []

    async def _fake_run_agent(request: RunRequest):
        if "Pre-Flight Reporter" in request.name:
            return IssueOutput(number=99, labels=["ready-for-human"])
        if "Implement Agent" in request.name:
            implementer_calls.append(request.name)
        return CompletionOutput()

    with pytest.raises(SystemExit) as exc_info:
        _run(
            tmp_path,
            agent_runner=FakeAgentRunner(
                side_effect=_fake_run_agent,
                preflight_responses=[
                    [_preflight_failure("ruff", "ruff check .", "E501")]
                ],
            ),
            github_service=_make_github_svc_hitl(),
        )

    assert exc_info.value.code != 0, "Exit code must be non-zero for HITL"
    assert implementer_calls == [], (
        f"No Implement Agent must be spawned on HITL; got {implementer_calls}"
    )


def test_preflight_failure_only_first_check_acted_on(tmp_path):
    """When multiple preflight checks fail, only the first (by order) must be acted on."""
    preflight_issue_calls: list[dict] = []

    async def _fake_run_agent(request: RunRequest):
        if "Pre-Flight Reporter" in request.name:
            preflight_issue_calls.append(
                {"name": request.name, "scope_args": request.prompt.scope_args or {}}
            )
            return IssueOutput(number=10, labels=["ready-for-human"])
        return CompletionOutput()

    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            agent_runner=FakeAgentRunner(
                side_effect=_fake_run_agent,
                preflight_responses=[
                    [
                        _preflight_failure("ruff", "ruff check .", "ruff error"),
                        _preflight_failure("mypy", "mypy .", "mypy error"),
                        _preflight_failure("pytest", "pytest", "pytest error"),
                    ]
                ],
            ),
            github_service=_make_github_svc_hitl(),
        )

    assert len(preflight_issue_calls) == 1, (
        f"Only one preflight-issue agent must be spawned; got {len(preflight_issue_calls)}"
    )
    args = preflight_issue_calls[0]["scope_args"]
    assert args.get("CHECK_NAME") == "ruff", (
        f"Must act on first check (ruff); got CHECK_NAME={args.get('CHECK_NAME')!r}"
    )
    assert args.get("COMMAND") == "ruff check .", (
        f"Must use first check's command; got {args.get('COMMAND')!r}"
    )


# ── Issue-409: sleep on usage limit instead of exiting ───────────────────────


def test_usage_limit_sleeps_instead_of_exiting(tmp_path):
    """When AbortedUsageLimit is received, run() must sleep instead of calling sys.exit()."""
    mock_github = _make_github_svc()
    mock_github.get_open_issues.side_effect = [
        [
            {
                "number": 1,
                "title": "Default Issue",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            },
            {
                "number": 2,
                "title": "Default Issue 2",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            },
        ],
        [],
    ]

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
            )
        raise UsageLimitError(reset_time=None)

    with patch("time.sleep") as mock_sleep:
        _run(
            tmp_path,
            _fake_run_agent,
            github_service=mock_github,
            max_iterations=2,
        )

    mock_sleep.assert_called_once()
    assert mock_sleep.call_args[0][0] > 0


def test_usage_limit_loop_continues_after_sleep_and_sets_slept_once(tmp_path):
    """After sleeping, the next iteration must see slept_once=True and skip until_sleep improve."""
    mock_github = _make_github_svc()
    mock_github.get_open_issues.side_effect = [
        [
            {
                "number": 1,
                "title": "Default Issue",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            },
            {
                "number": 2,
                "title": "Default Issue 2",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            },
        ],
        [],
    ]

    async def _fake_run_agent(request: RunRequest):
        if "Improve Agent" in request.name:
            raise AssertionError(
                "Improve Agent must not run after a usage-limit sleep in until_sleep mode"
            )
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
            )
        raise UsageLimitError(reset_time=None)

    with patch("time.sleep"):
        _run(
            tmp_path,
            _fake_run_agent,
            github_service=mock_github,
            max_iterations=2,
            improve_mode="until_sleep",
        )

    assert mock_github.get_open_issues.call_count == 2


def test_usage_limit_error_not_written_to_errors_log(tmp_path):
    """UsageLimitError must not be logged to errors.log."""
    logs_dir = tmp_path / "pycastle" / "logs"
    logs_dir.mkdir(parents=True)
    errors_log = logs_dir / "errors.log"

    mock_github = _make_github_svc()
    mock_github.get_open_issues.side_effect = [
        [
            {
                "number": 1,
                "title": "Default Issue",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            },
            {
                "number": 2,
                "title": "Default Issue 2",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            },
        ],
        [],
    ]

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
            )
        raise UsageLimitError(reset_time=None)

    with patch("time.sleep"):
        _run(
            tmp_path,
            _fake_run_agent,
            github_service=mock_github,
            logs_dir=logs_dir,
            max_iterations=2,
        )

    assert not errors_log.exists() or errors_log.read_text() == "", (
        "UsageLimitError must not be written to errors.log"
    )


# ── Preflight-phase usage-limit handling ─────────────────────────────────────


def test_usage_limit_in_preflight_sleeps_instead_of_crashing(tmp_path):
    """UsageLimitError raised during preflight (Pre-Flight Reporter) must be caught and
    routed through the orchestrator's sleep-and-retry path rather than crashing."""
    mock_github = _make_github_svc_afk()
    mock_github.get_open_issues.side_effect = [
        [
            {
                "number": 1,
                "title": "Default Issue",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            },
            {
                "number": 2,
                "title": "Default Issue 2",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            },
        ],
        [],
    ]

    async def _fake_run_agent(request: RunRequest):
        if "Pre-Flight Reporter" in request.name:
            raise UsageLimitError(reset_time=None)
        return CompletionOutput()

    with patch("time.sleep") as mock_sleep:
        _run(
            tmp_path,
            agent_runner=FakeAgentRunner(
                side_effect=_fake_run_agent,
                preflight_responses=[
                    [_preflight_failure("ruff", "ruff check .", "E501")],
                    [],
                ],
            ),
            github_service=mock_github,
            max_iterations=2,
        )

    mock_sleep.assert_called_once()
    assert mock_sleep.call_args[0][0] > 0


# ── Issue-194: skip Planner when no ready-for-agent issues exist ──────────────


def test_planner_not_invoked_when_no_ready_for_agent_issues(tmp_path):
    """Planner must not be spawned when there are no open issues."""
    agent_names: list[str] = []

    async def _fake_run_agent(request: RunRequest):
        agent_names.append(request.name)
        return CompletionOutput()

    mock_github = _make_github_svc()
    mock_github.get_open_issues.return_value = []

    _run(tmp_path, _fake_run_agent, github_service=mock_github)

    assert "Plan Agent" not in agent_names, (
        f"Plan Agent must not be invoked when no ready-for-agent issues exist; agents={agent_names}"
    )


def test_skip_message_emitted_before_any_agent_when_no_issues(tmp_path, capsys):
    """'No unblocked issues with label ... found. Skipping.' must be printed and no agent must run."""
    agent_names: list[str] = []

    async def _fake_run_agent(request: RunRequest):
        agent_names.append(request.name)
        return CompletionOutput()

    mock_github = _make_github_svc()
    mock_github.get_open_issues.return_value = []

    _run(tmp_path, _fake_run_agent, github_service=mock_github)

    out = capsys.readouterr().out
    assert "No unblocked issues with label" in out and "found. Skipping." in out, (
        f"Skip message not printed; stdout={out!r}"
    )
    assert agent_names == [], (
        f"No agents must run when there are no matching issues; got {agent_names}"
    )


def test_planner_invoked_when_ready_for_agent_issues_exist(tmp_path):
    """Planner must be spawned when there are open issues."""
    agent_names: list[str] = []

    async def _fake_run_agent(request: RunRequest):
        agent_names.append(request.name)
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Do thing", "body": "x" * 100, "comments": []}]
            )
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return CompletionOutput()

    mock_github = _make_github_svc()

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        github_service=mock_github,
    )

    assert "Plan Agent" in agent_names, (
        f"Plan Agent must be invoked when ready-for-agent issues exist; agents={agent_names}"
    )


# ── Issue-200: planner receives READY_FOR_AGENT_ISSUES_JSON (not ISSUE_LABEL) ─


def test_planner_receives_ready_for_agent_issues_json_not_issue_label(tmp_path):
    """run() must pass READY_FOR_AGENT_ISSUES_JSON (not ISSUE_LABEL) in planner prompt_args."""
    captured_planner_args: dict = {}

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            captured_planner_args.update(request.prompt.scope_args or {})
            return _plan_output(
                [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
            )
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return CompletionOutput()

    mock_github = _make_github_svc()
    mock_github.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Fix thing",
            "body": "Blocked by #99\n" + "Do the work. " + "x" * 90,
            "labels": ["behavior-slice"],
            "comments": [],
        },
        {
            "number": 2,
            "title": "Another issue",
            "body": "x" * 100,
            "labels": ["behavior-slice"],
        },
    ]

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        github_service=mock_github,
    )

    assert "READY_FOR_AGENT_ISSUES_JSON" in captured_planner_args, (
        "Planner must receive READY_FOR_AGENT_ISSUES_JSON in prompt_args"
    )
    assert "ALL_OPEN_ISSUES_JSON" in captured_planner_args, (
        "Planner must receive ALL_OPEN_ISSUES_JSON in prompt_args"
    )
    assert "READY_FOR_AGENT_LABEL" not in captured_planner_args, (
        "Planner must not receive READY_FOR_AGENT_LABEL in prompt_args"
    )
    assert (
        "Blocked by #99" not in captured_planner_args["READY_FOR_AGENT_ISSUES_JSON"]
    ), "Stale blocker reference must be stripped from READY_FOR_AGENT_ISSUES_JSON"


# ── Issue-204: cfg injection ──────────────────────────────────────────────────


def test_run_stops_after_max_iterations_from_cfg(tmp_path):
    """run() with cfg=Config(max_iterations=2) must stop after 2 iteration cycles."""
    planner_calls = [0]

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            planner_calls[0] += 1
            if planner_calls[0] < 2:
                return _plan_output(
                    [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
                )
            return _plan_output([])
        return CompletionOutput()

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        max_iterations=2,
    )

    assert planner_calls[0] == 2, f"Expected 2 planner calls; got {planner_calls[0]}"


def test_run_limits_concurrency_to_max_parallel_from_cfg(tmp_path):
    """run() with max_parallel=2 must not exceed 2 concurrent implementers."""
    active_count = 0
    max_active = 0

    async def _fake_run_agent(request: RunRequest):
        nonlocal active_count, max_active
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": i, "title": f"Issue {i}"} for i in range(1, 6)]
            )
        active_count += 1
        max_active = max(max_active, active_count)
        await asyncio.sleep(0.01)
        active_count -= 1
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return CompletionOutput()

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(),
        github_service=_make_github_svc(numbers=list(range(1, 6))),
        max_parallel=2,
    )

    assert max_active <= 2, f"Expected at most 2 concurrent; max was {max_active}"


# ── GithubService eager construction + check_auth preflight ─────────────────


def test_run_calls_check_auth_before_iteration(tmp_path):
    """run() must call check_auth on the GithubService before doing iteration work."""
    call_order: list[str] = []
    mock_github = _make_github_svc()
    mock_github.check_auth.side_effect = lambda: (
        call_order.append("check_auth") or "octocat"
    )
    mock_github.get_open_issues.side_effect = lambda label: (
        call_order.append("get_open_issues") or []
    )

    async def _fake_run_agent(request: RunRequest):
        return CompletionOutput()

    _run(tmp_path, _fake_run_agent, github_service=mock_github)

    assert call_order[0] == "check_auth"
    assert "get_open_issues" in call_order


def test_run_prints_github_authenticated_login_at_startup(tmp_path):
    """run() must print a GitHub-specific auth line with the authenticated login."""
    recording = RecordingStatusDisplay()
    mock_github = _make_github_svc()
    mock_github.check_auth.return_value = "octocat"
    mock_github.get_open_issues.return_value = []

    async def _fake_run_agent(request: RunRequest):
        return CompletionOutput()

    _run(
        tmp_path, _fake_run_agent, github_service=mock_github, status_display=recording
    )

    auth_prints = [
        c
        for c in recording.calls
        if c[0] == "print" and isinstance(c[2], str) and "GitHub auth:" in c[2]
    ]
    assert len(auth_prints) == 1, f"Expected one auth-summary print; got {auth_prints}"
    assert auth_prints[0][2] == "GitHub auth: authenticated as @octocat"


def test_run_exits_when_github_auth_error(tmp_path, capsys):
    """run() must exit 1 and print the auth error body when check_auth fails."""
    mock_github = _make_github_svc()
    mock_github.check_auth.side_effect = GithubAuthError(
        "auth failed", status=401, body="Bad credentials"
    )

    with pytest.raises(SystemExit) as exc_info:
        _run(tmp_path, github_service=mock_github)
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "Bad credentials" in err


def test_run_exits_when_github_auth_retry_exhausts(tmp_path, capsys):
    """Startup GitHub retry exhaustion must print a terminal error and exit 1."""
    mock_github = _make_github_svc()
    mock_github.check_auth.side_effect = OperatorActionableGithubError(
        "GitHub API GET /user failed after 4 attempts: "
        "GitHub API GET /user returned 502: Bad Gateway",
        method="GET",
        path="/user",
        attempt_count=4,
        cause=GithubAPIError(
            "GitHub API GET /user returned 502: Bad Gateway",
            status=502,
            body="Bad Gateway",
            method="GET",
            path="/user",
        ),
    )

    with pytest.raises(SystemExit) as exc_info:
        _run(tmp_path, github_service=mock_github)

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "GitHub request retry limit reached:" in err
    assert "GET /user failed after 4 attempts" in err


def test_run_exits_when_gh_token_missing(tmp_path, capsys):
    """run() without an injected github_service must exit 1 when GH_TOKEN is missing."""
    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(
            run(
                {},
                tmp_path,
                git_service=_make_git_svc(),
            )
        )
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "GH_TOKEN" in err


def test_run_with_empty_repo_root_completes(tmp_path):
    """run() with empty repo_root completes without error using default config."""

    async def _fake_run_agent(request: RunRequest):
        return _plan_output([])

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(),
        github_service=_make_github_svc(),
    )


# ── Issue-332: unconfigured git identity detected at startup ──────────────────


def _make_git_svc_no_user_name():
    svc = _make_git_svc()
    svc.get_user_name.side_effect = GitCommandError(
        "git config user.name failed", returncode=1, stderr=""
    )
    return svc


def _make_git_svc_no_user_email():
    svc = _make_git_svc()
    svc.get_user_email.side_effect = GitCommandError(
        "git config user.email failed", returncode=1, stderr=""
    )
    return svc


def test_run_exits_with_code_1_when_git_user_name_not_configured(tmp_path):
    """run() must exit 1 when git user.name is not set."""
    with pytest.raises(SystemExit) as exc_info:
        _run(
            tmp_path,
            git_service=_make_git_svc_no_user_name(),
            github_service=_make_github_svc(),
        )
    assert exc_info.value.code == 1


def test_run_exits_with_code_1_when_git_user_email_not_configured(tmp_path):
    """run() must exit 1 when git user.email is not set."""
    with pytest.raises(SystemExit) as exc_info:
        _run(
            tmp_path,
            git_service=_make_git_svc_no_user_email(),
            github_service=_make_github_svc(),
        )
    assert exc_info.value.code == 1


def test_run_prints_git_config_instruction_when_identity_not_configured(
    tmp_path, capsys
):
    """run() must print both git config commands to stderr when user identity is missing."""
    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            git_service=_make_git_svc_no_user_name(),
            github_service=_make_github_svc(),
        )
    err = capsys.readouterr().err
    assert "git config --global user.name" in err
    assert "git config --global user.email" in err


def test_run_no_agents_start_when_git_identity_not_configured(tmp_path):
    """run() must not spawn any agents when git identity is missing."""
    agents_started: list[str] = []

    async def _fake_run_agent(request: RunRequest):
        agents_started.append(request.name)
        return CompletionOutput()

    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            _fake_run_agent,
            git_service=_make_git_svc_no_user_name(),
            github_service=_make_github_svc(),
        )

    assert agents_started == [], (
        f"No agents must start when git identity missing; got {agents_started}"
    )


def test_run_passes_plan_override_model_and_effort_to_planner(tmp_path):
    """run() with plan_override must pass its model and effort to the Planner agent."""
    captured_planner: dict = {}

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            captured_planner["model"] = request.model
            captured_planner["effort"] = request.effort
            return _plan_output([])
        return CompletionOutput()

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        plan_override=StageOverride(model="claude-haiku-4-5", effort="low"),
    )

    assert captured_planner.get("model") == "claude-haiku-4-5"
    assert captured_planner.get("effort") == "low"


# ── Issue-206: worktree_sha set at iteration start; no post-merge host checks ──


def test_worktree_sha_set_at_iteration_start(tmp_path):
    """get_head_sha must be called before the Planner agent fires each iteration."""
    call_order: list[str] = []

    mock_git = _make_git_svc(try_merge_side_effect=[True])
    original_get_head_sha = mock_git.get_head_sha.side_effect

    def _tracking_get_head_sha(repo_path):
        call_order.append("get_head_sha")
        if original_get_head_sha is not None:
            return original_get_head_sha(repo_path)
        return "abc123"

    mock_git.get_head_sha.side_effect = _tracking_get_head_sha

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            call_order.append("Planner")
            return _plan_output(
                [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
            )
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return CompletionOutput()

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=mock_git,
        github_service=_make_github_svc(),
    )

    assert "get_head_sha" in call_order, "get_head_sha must be called"
    assert "Planner" in call_order, "Planner must be called"
    first_sha = call_order.index("get_head_sha")
    first_planner = call_order.index("Planner")
    assert first_sha < first_planner, (
        f"get_head_sha must be called before Planner; order={call_order}"
    )


def test_worktree_sha_refreshed_each_iteration(tmp_path):
    """get_head_sha must be called before each Planner call across multiple iterations.

    run_issue also calls get_safe_sha() (and thus get_head_sha) for its implementer
    worktree, so there are more calls than one per iteration — the ordering invariant
    is what matters: every Planner call must be preceded by at least one get_head_sha.
    """
    call_order: list[str] = []
    planner_count = [0]

    mock_git = _make_git_svc(try_merge_side_effect=[True])
    mock_git.get_head_sha.side_effect = lambda _: (
        call_order.append("get_head_sha") or "sha"
    )

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            planner_count[0] += 1
            call_order.append(f"Planner-{planner_count[0]}")
            if planner_count[0] == 1:
                return _plan_output(
                    [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
                )
            return _plan_output([])
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return CompletionOutput()

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=mock_git,
        github_service=_make_github_svc(),
        max_iterations=2,
    )

    planner_indices = [i for i, e in enumerate(call_order) if e.startswith("Planner")]
    assert len(planner_indices) == 2, (
        f"Planner must be called twice; order={call_order}"
    )
    for planner_idx in planner_indices:
        preceding_sha = any(
            e == "get_head_sha" and i < planner_idx for i, e in enumerate(call_order)
        )
        assert preceding_sha, f"get_head_sha must precede Planner; order={call_order}"


# ── Issue-206: worktree SHA + full iteration path ─────────────────────────────


def test_run_full_iteration_cold_path(git_repo):
    """run() executes a full iteration: preflight→plan→implement→merge, and closes the issue."""
    import subprocess

    branch = "pycastle/issue-1"
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-b", branch],
        check=True,
        capture_output=True,
    )
    (git_repo / "feature.txt").write_text("feature")
    (git_repo / "pyproject.toml").write_text(
        "[project]\nname = 't'\nversion = '0.0.1'\n"
    )
    subprocess.run(
        ["git", "-C", str(git_repo), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(git_repo), "commit", "-m", "add feature"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "main"],
        check=True,
        capture_output=True,
    )

    closed_issues: list[int] = []
    mock_github = _make_github_svc()
    mock_github.close_issue_with_parents.side_effect = lambda n: closed_issues.append(n)

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix thing", "body": "x" * 100, "comments": []}]
            )
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return CompletionOutput()

    (git_repo / "pycastle").mkdir(exist_ok=True)
    (git_repo / "pycastle" / "config.py").write_text(
        "max_parallel = 4\nmax_iterations = 1\n"
    )
    asyncio.run(
        run(
            {},
            git_repo,
            agent_runner=FakeAgentRunner(side_effect=_fake_run_agent),
            github_service=mock_github,
        )
    )

    assert 1 in closed_issues, (
        f"Issue #1 must be closed after merge; closed={closed_issues}"
    )


# ── Issue-52: Planner preflight error HITL/AFK routing ───────────────────────


def test_planner_preflight_error_spawns_no_implementers(tmp_path):
    """On plan-sandbox PreflightError with HITL verdict, run must exit immediately."""

    async def _fake_run_agent(request: RunRequest):
        return IssueOutput(number=77, labels=["ready-for-human"])

    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            agent_runner=FakeAgentRunner(
                side_effect=_fake_run_agent,
                preflight_responses=[
                    [_preflight_failure("ruff", "ruff check .", "E501 line too long")]
                ],
            ),
            github_service=_make_github_svc_hitl(),
        )


def test_planner_preflight_error_message_names_issue_number(tmp_path, capsys):
    """HITL preflight failure must print a message referencing the filed issue number."""

    async def _fake_run_agent(request: RunRequest):
        return IssueOutput(number=88, labels=["ready-for-human"])

    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            agent_runner=FakeAgentRunner(
                side_effect=_fake_run_agent,
                preflight_responses=[
                    [_preflight_failure("ruff", "ruff check .", "E501 line too long")]
                ],
            ),
            github_service=_make_github_svc_hitl(),
        )

    out = capsys.readouterr().out
    assert "88" in out, f"Output must reference the filed issue number; got: {out!r}"


# ── Issue-407: no pycastle startup row ───────────────────────────────────────


def test_startup_does_not_register_pycastle_row(tmp_path):
    """run() must not register a 'pycastle' status row — the orchestrator is not an agent."""
    recording = RecordingStatusDisplay()

    async def _fake_run_agent(request: RunRequest):
        return _plan_output([])

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        status_display=recording,
    )

    pycastle_registers = [
        c for c in recording.calls if c[:2] == ("register", "pycastle")
    ]
    assert pycastle_registers == [], (
        f"No 'pycastle' register calls expected; got {pycastle_registers}"
    )


def test_startup_does_not_remove_pycastle_row(tmp_path):
    """run() must not remove a 'pycastle' status row — the orchestrator is not an agent."""
    recording = RecordingStatusDisplay()

    async def _fake_run_agent(request: RunRequest):
        return _plan_output([])

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        status_display=recording,
    )

    pycastle_removes = [c for c in recording.calls if c[:2] == ("remove", "pycastle")]
    assert pycastle_removes == [], (
        f"No 'pycastle' remove calls expected; got {pycastle_removes}"
    )


def test_startup_does_not_use_pycastle_caller_on_git_identity_failure(tmp_path):
    """run() must not emit any 'pycastle' register or remove calls when git identity check fails."""
    recording = RecordingStatusDisplay()

    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            git_service=_make_git_svc_no_user_name(),
            github_service=_make_github_svc(),
            status_display=recording,
        )

    pycastle_calls = [c for c in recording.calls if c[1] == "pycastle"]
    assert pycastle_calls == [], (
        f"No 'pycastle' calls expected on git identity failure; got {pycastle_calls}"
    )


def test_startup_does_not_use_pycastle_caller_on_credentials_failure(tmp_path):
    """run() must not emit any 'pycastle' register or remove calls when GH_TOKEN is missing."""
    recording = RecordingStatusDisplay()

    with pytest.raises(SystemExit):
        asyncio.run(
            run(
                {},
                tmp_path,
                git_service=_make_git_svc(),
                status_display=recording,
            )
        )

    pycastle_calls = [c for c in recording.calls if c[1] == "pycastle"]
    assert pycastle_calls == [], (
        f"No 'pycastle' calls expected on credentials failure; got {pycastle_calls}"
    )


def test_iteration_header_uses_anonymous_caller(tmp_path):
    """Iteration boundary header must use '' as caller so it prints without a bracket prefix."""
    recording = RecordingStatusDisplay()

    async def _fake_run_agent(request: RunRequest):
        return _plan_output([])

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        status_display=recording,
    )

    header_calls = [
        c for c in recording.calls if c[0] == "print" and "=== Iteration" in str(c[2])
    ]
    assert header_calls, "Iteration boundary header must be printed"
    for call in header_calls:
        assert call[1] == "", f"Iteration header must use '' as caller; got {call[1]!r}"


# ── Issue-504: service registry failover ─────────────────────────────────────


def test_usage_limit_with_service_available_does_not_sleep(tmp_path):
    """When service.is_available() is True, orchestrator must not sleep on AbortedUsageLimit."""
    mock_github = _make_github_svc()
    mock_github.get_open_issues.side_effect = [
        [
            {
                "number": 1,
                "title": "Default Issue",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            },
            {
                "number": 2,
                "title": "Default Issue 2",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            },
        ],
        [],
    ]

    svc = _FakeService(available=True)

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
            )
        raise UsageLimitError(reset_time=None)

    with patch("time.sleep") as mock_sleep:
        _run(
            tmp_path,
            _fake_run_agent,
            github_service=mock_github,
            service_registry=ServiceRegistry({"claude": svc}),
            max_iterations=2,
        )

    mock_sleep.assert_not_called()


def test_pool_summary_printed_at_startup_with_both_accounts(tmp_path):
    from pycastle.services.runtime_services import ClaudeService

    mock_github = _make_github_svc()
    mock_github.get_open_issues.return_value = []

    svc = ClaudeService(accounts=[("account 2", "tok-s"), ("account 1", "tok-p")])
    recording = RecordingStatusDisplay()

    async def _fake_run_agent(request: RunRequest):
        return CompletionOutput()

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=mock_github,
        service_registry=ServiceRegistry({"claude": svc}),
        status_display=recording,
    )

    msgs = [c[2] for c in recording.calls if c[0] == "print"]
    assert any(
        "Claude accounts: account 2 (active), account 1 (standby)" in str(m)
        for m in msgs
    )


def test_pool_summary_printed_at_startup_with_primary_only(tmp_path):
    from pycastle.services.runtime_services import ClaudeService

    mock_github = _make_github_svc()
    mock_github.get_open_issues.return_value = []

    svc = ClaudeService(accounts=[("account 1", "tok-p")])
    recording = RecordingStatusDisplay()

    async def _fake_run_agent(request: RunRequest):
        return CompletionOutput()

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=mock_github,
        service_registry=ServiceRegistry({"claude": svc}),
        status_display=recording,
    )

    msgs = [c[2] for c in recording.calls if c[0] == "print"]
    assert any("Claude accounts: account 1 (active)" in str(m) for m in msgs)


def test_codex_auth_summary_printed_at_startup(tmp_path):
    mock_github = _make_github_svc()
    mock_github.get_open_issues.return_value = []
    recording = RecordingStatusDisplay()

    async def _fake_run_agent(request: RunRequest):
        return CompletionOutput()

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=mock_github,
        service_registry=ServiceRegistry({"codex": _FakeService()}),
        status_display=recording,
    )

    msgs = [c[2] for c in recording.calls if c[0] == "print"]
    assert any("Codex auth: local auth available" in str(m) for m in msgs)


def test_run_does_not_resolve_preflight_issue_override_before_failure_dispatch(
    tmp_path,
):
    mock_github = _make_github_svc()
    mock_github.get_open_issues.return_value = []
    registry = _RecordingServiceRegistry({"codex": _FakeService()})
    preflight_override = StageOverride(
        service="codex",
        model="gpt-5.2",
        effort="low",
    )

    _run(
        tmp_path,
        agent_runner=FakeAgentRunner([], preflight_responses=[[]]),
        github_service=mock_github,
        service_registry=registry,
        preflight_issue_override=preflight_override,
    )

    assert ("codex", "gpt-5.2", "low") not in registry.resolve_calls


# ── ensure_session_excludes ───────────────────────────────────────────────────


def test_ensure_session_excludes_creates_exclude_file_with_entries(tmp_path):
    """ensure_session_excludes appends .pycastle-session/ and .claude/ to .git/info/exclude."""
    git_info = tmp_path / ".git" / "info"
    git_info.mkdir(parents=True)

    ensure_session_excludes(tmp_path)

    content = (git_info / "exclude").read_text()
    assert ".pycastle-session/" in content
    assert ".claude/" in content


def test_ensure_session_excludes_is_idempotent(tmp_path):
    """Calling ensure_session_excludes twice must not duplicate entries."""
    git_info = tmp_path / ".git" / "info"
    git_info.mkdir(parents=True)

    ensure_session_excludes(tmp_path)
    ensure_session_excludes(tmp_path)

    content = (git_info / "exclude").read_text()
    assert content.count(".pycastle-session/") == 1
    assert content.count(".claude/") == 1


def test_ensure_session_excludes_preserves_existing_content(tmp_path):
    """ensure_session_excludes must not overwrite pre-existing exclude rules."""
    git_info = tmp_path / ".git" / "info"
    git_info.mkdir(parents=True)
    exclude_file = git_info / "exclude"
    exclude_file.write_text("# Existing rule\n*.log\n")

    ensure_session_excludes(tmp_path)

    content = exclude_file.read_text()
    assert "*.log" in content
    assert ".pycastle-session/" in content


def test_ensure_session_excludes_noop_when_git_dir_absent(tmp_path):
    """ensure_session_excludes must not raise when .git/info/ does not exist."""
    ensure_session_excludes(tmp_path)  # should not raise


# ── Issue-633: preflight gate wired into improve/plan ────────────────────────


def test_idle_iteration_skips_preflight_gate(tmp_path):
    """When there are no open issues and improve_mode is None, git pull must never be called."""
    mock_git = _make_git_svc()
    mock_github = _make_github_svc()
    mock_github.get_open_issues.return_value = []

    _run(tmp_path, github_service=mock_github, git_service=mock_git)

    mock_git.pull_with_merge_fallback.assert_not_called()


def test_in_flight_only_iteration_planning_runs_preflight_gate_once(tmp_path):
    """When all open issues are in-flight, planning runs the preflight gate once
    and still skips spawning the Planner."""
    pull_call_count = [0]
    mock_git = _make_git_svc(try_merge_side_effect=[True])
    mock_git.verify_ref_exists.return_value = True  # branch exists → in-flight

    original_pull = mock_git.pull_with_merge_fallback.side_effect

    def _tracking_pull(repo_path):
        pull_call_count[0] += 1
        if original_pull is not None:
            return original_pull(repo_path)

    mock_git.pull_with_merge_fallback.side_effect = _tracking_pull
    mock_github = _make_github_svc(numbers=[1])

    async def _fake_run_agent(request: RunRequest):
        return CompletionOutput()

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=mock_git,
        github_service=mock_github,
    )

    assert pull_call_count[0] == 1, (
        "In-flight planning path must call pull_with_merge_fallback exactly once"
    )


def test_preflight_afk_from_planning_routes_to_implement_same_iteration(tmp_path):
    """PreflightAFK returned from planning must route to implement in the same iteration,
    calling the Implement Agent for the filed issue without invoking the Plan Agent."""
    agent_names: list[str] = []

    async def _fake_run_agent(request: RunRequest):
        agent_names.append(request.name)
        if "Pre-Flight Reporter" in request.name:
            return IssueOutput(number=42, labels=["ready-for-agent", "behavior-slice"])
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return CompletionOutput()

    mock_github = _make_github_svc_afk()
    mock_github.get_issue.return_value = {
        "number": 42,
        "title": "Preflight fix",
        "body": "x" * 100,
        "comments": [],
        "labels": ["behavior-slice"],
    }

    _run(
        tmp_path,
        agent_runner=FakeAgentRunner(
            side_effect=_fake_run_agent,
            preflight_responses=[[_preflight_failure("ruff", "ruff check .", "E501")]],
        ),
        github_service=mock_github,
        git_service=_make_git_svc(try_merge_side_effect=[True]),
    )

    assert "Plan Agent" not in agent_names, "Plan Agent must not be called on AFK path"
    assert any("Implement Agent" in n for n in agent_names), (
        "Implement Agent must be called for the AFK issue"
    )


def test_preflight_hitl_from_planning_returns_aborted_hitl(tmp_path):
    """PreflightHITL returned from planning must abort with sys.exit non-zero."""

    async def _fake_run_agent(request: RunRequest):
        return IssueOutput(number=55, labels=["ready-for-human"])

    with pytest.raises(SystemExit) as exc_info:
        _run(
            tmp_path,
            agent_runner=FakeAgentRunner(
                side_effect=_fake_run_agent,
                preflight_responses=[
                    [_preflight_failure("ruff", "ruff check .", "E501")]
                ],
            ),
            github_service=_make_github_svc_hitl(),
        )

    assert exc_info.value.code != 0


def test_improve_and_plan_share_preflight_cache(tmp_path):
    """When improve runs then planning runs in the same iteration,
    PreflightCache.get_safe_sha must only run preflight once (the second call
    hits the cache)."""
    mock_git = _make_git_svc(try_merge_side_effect=[True])
    mock_github = MagicMock(spec=GithubService)
    mock_github.get_open_issues.side_effect = [
        [],  # first call → triggers improve path
        [
            {
                "number": 7,
                "title": "Filed issue",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            }
        ],  # after improve
    ]
    mock_github.get_all_open_issues_lightweight.return_value = []
    mock_github.get_issue.return_value = {
        "number": 5,
        "title": "PRD",
        "body": "x" * 100,
        "comments": [],
    }
    mock_github.get_issue_comments.return_value = []

    async def _fake_run_agent(request: RunRequest):
        if "Scan Agent" in request.name:
            return IssueOutput(number=5, labels=[])  # 01-scan picks a candidate
        if "PRD Agent" in request.name:
            return IssueOutput(number=5, labels=[])  # 02-prd
        if "Slice Agent" in request.name:
            return CompletionOutput()  # 03-issues files sub-issues
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return CompletionOutput()

    fake = FakeAgentRunner(
        side_effect=_fake_run_agent,
        preflight_responses=[[]],  # exactly one preflight pass
    )

    _run(
        tmp_path,
        agent_runner=fake,
        github_service=mock_github,
        git_service=mock_git,
        improve_mode="endless",
    )

    assert len(fake.preflight_calls) == 1, (
        f"preflight must run exactly once across improve+plan; "
        f"got {len(fake.preflight_calls)} calls"
    )


# ── AbortedTimeout: orchestrator continues without sleep ─────────────────────


def test_orchestrator_aborted_timeout_continues_to_next_iteration_without_sleep(
    tmp_path,
):
    """AbortedTimeout from run_iteration causes the orchestrator to start the next
    iteration immediately without calling time.sleep and without exiting the process."""
    mock_github = _make_github_svc(numbers=[1, 2])
    call_count = [0]

    async def _fake_run_agent(req: RunRequest):
        call_count[0] += 1
        raise AgentTimeoutError("timeout")

    mock_github.get_open_issues.side_effect = [
        [
            {
                "number": 1,
                "title": "Fix",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            },
            {
                "number": 2,
                "title": "Fix B",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            },
        ],
        [],
    ]

    with patch("time.sleep") as mock_sleep:
        _run(
            tmp_path,
            _fake_run_agent,
            github_service=mock_github,
            max_iterations=2,
        )

    mock_sleep.assert_not_called()
    assert call_count[0] >= 1


# ── Issue-789: per-stage fallback triple consumption ─────────────────────────


def test_exhausted_primary_dispatches_with_fallback_triple(tmp_path):
    """When the stage's primary service is exhausted and the fallback service is
    available, the dispatch uses the fallback's (service, model, effort) triple."""
    captured: list[dict] = []

    claude_svc = _FakeService(available=False, wake_time=datetime(2026, 6, 1, 12, 0))
    codex_svc = _FakeService(available=True)

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
            )
        if "Implement Agent" in request.name:
            captured.append(
                {
                    "service": request.service,
                    "model": request.model,
                    "effort": request.effort,
                }
            )
            return CompletionOutput()
        return CompletionOutput()

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        service_registry=ServiceRegistry({"claude": claude_svc, "codex": codex_svc}),
        implement_override=StageOverride(
            service="claude",
            model="primary-model",
            effort="low",
            fallback=StageOverride(
                service="codex", model="fallback-model", effort="high"
            ),
        ),
    )

    assert len(captured) == 1, f"Expected one implement dispatch; got {len(captured)}"
    assert captured[0]["service"] == "codex", (
        f"Expected fallback service 'codex'; got {captured[0]['service']!r}"
    )
    assert captured[0]["model"] == "fallback-model", (
        f"Expected fallback model 'fallback-model'; got {captured[0]['model']!r}"
    )
    assert captured[0]["effort"] == "high", (
        f"Expected fallback effort 'high'; got {captured[0]['effort']!r}"
    )


def test_opencode_timeout_usage_exhaustion_switches_to_fallback_instead_of_sleeping(
    tmp_path,
):
    captured: list[tuple[str, str]] = []
    issues = [
        {
            "number": 1,
            "title": "Fix",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]

    class _MarkingOpencodeService(_FakeService):
        @property
        def name(self) -> str:
            return "opencode"

        def mark_exhausted(self, reset_time) -> None:
            del reset_time
            self._available = False

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(issues)
        if request.name.startswith("Implement Agent"):
            captured.append((request.service, request.name))
            if request.service == "opencode":
                opencode.mark_exhausted(None)
                raise UsageLimitError(
                    reset_time=None, raw_message=None, provider="opencode"
                )
            return CompletionOutput()
        return CompletionOutput()

    opencode = _MarkingOpencodeService(available=True)
    codex = _FakeService(available=True)

    with patch("time.sleep") as mock_sleep:
        _run(
            tmp_path,
            _fake_run_agent,
            github_service=_make_github_svc(numbers=[1]),
            git_service=_make_git_svc(try_merge_side_effect=[True]),
            service_registry=ServiceRegistry({"opencode": opencode, "codex": codex}),
            implement_override=StageOverride(
                service="opencode",
                model="kimi-k2.6",
                effort="low",
                fallback=StageOverride(service="codex", effort="high", model="gpt-5.2"),
            ),
            max_iterations=2,
        )

    assert captured == [
        ("opencode", "Implement Agent #1"),
        ("codex", "Implement Agent #1"),
    ]
    mock_sleep.assert_not_called()


def test_opencode_timeout_usage_exhaustion_sleeps_until_marked_wake_without_fallback(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    wake_time = datetime(2026, 1, 1, 13, 45, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "pycastle.iteration.orchestrator._time_module.now_local", lambda: now
    )

    class _MarkingOpencodeService(_FakeService):
        def __init__(self) -> None:
            super().__init__(available=True)

        @property
        def name(self) -> str:
            return "opencode"

        def mark_exhausted(self, reset_time) -> None:
            self._available = False
            self._wake_time = reset_time

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
            )
        if request.name.startswith("Implement Agent"):
            opencode.mark_exhausted(wake_time)
            raise UsageLimitError(
                reset_time=None,
                raw_message=None,
                provider="opencode",
            )
        return CompletionOutput()

    opencode = _MarkingOpencodeService()
    mock_github = _make_github_svc(numbers=[1])
    mock_github.get_open_issues.side_effect = [
        [
            {
                "number": 1,
                "title": "Issue 1",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            }
        ],
        [],
    ]

    with patch("time.sleep") as mock_sleep:
        _run(
            tmp_path,
            _fake_run_agent,
            github_service=mock_github,
            git_service=_make_git_svc(try_merge_side_effect=[True]),
            service_registry=ServiceRegistry(
                cast(dict[str, AgentService], {"opencode": opencode})
            ),
            implement_override=StageOverride(
                service="opencode",
                effort="low",
                model="deepseek-v4-flash",
            ),
            max_iterations=2,
        )

    mock_sleep.assert_called_once_with((wake_time - now).total_seconds())


def test_primary_takes_precedence_when_both_services_available(tmp_path):
    """Snap-back is automatic: when the primary service is available, dispatch uses
    the primary's (service, model, effort) even if the fallback is also available."""
    captured: list[dict] = []

    claude_svc = _FakeService(available=True)
    codex_svc = _FakeService(available=True)

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
            )
        if "Implement Agent" in request.name:
            captured.append(
                {
                    "service": request.service,
                    "model": request.model,
                    "effort": request.effort,
                }
            )
            return CompletionOutput()
        return CompletionOutput()

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        service_registry=ServiceRegistry({"claude": claude_svc, "codex": codex_svc}),
        implement_override=StageOverride(
            service="claude",
            model="primary-model",
            effort="low",
            fallback=StageOverride(
                service="codex", model="fallback-model", effort="high"
            ),
        ),
    )

    assert len(captured) == 1
    assert captured[0]["service"] == "claude", (
        "Primary available: dispatch must use primary service, not fallback"
    )
    assert captured[0]["model"] == "primary-model", (
        "Primary available: dispatch must use primary model, not fallback"
    )
    assert captured[0]["effort"] == "low", (
        "Primary available: dispatch must use primary effort, not fallback"
    )


def test_usage_limit_on_resolved_fallback_rechecks_full_stage_chain_before_sleep(
    tmp_path,
):
    """When a fallback candidate hits usage limit, the orchestrator must re-check the
    full configured stage chain before sleeping so a recovered higher-priority
    candidate is picked immediately."""
    captured: list[str] = []
    mock_github = _make_github_svc(numbers=[1])
    mock_github.get_open_issues.side_effect = [
        [
            {
                "number": 1,
                "title": "Fix",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            }
        ],
        [
            {
                "number": 1,
                "title": "Fix",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            }
        ],
    ]

    primary = _SequencedAvailabilityService([False, True])
    fallback = _SequencedAvailabilityService(
        [True, False],
        wake_time=datetime(2026, 1, 1, 15, 0, 0).astimezone(),
    )
    stable = _FakeService(available=True)

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
            )
        if "Implement Agent" in request.name:
            captured.append(request.service)
            if len(captured) == 1:
                raise UsageLimitError(reset_time=None)
            return CompletionOutput()
        return CompletionOutput()

    with patch("time.sleep") as mock_sleep:
        _run(
            tmp_path,
            _fake_run_agent,
            github_service=mock_github,
            git_service=_make_git_svc(try_merge_side_effect=[True, True]),
            service_registry=ServiceRegistry(
                {"primary": primary, "fallback": fallback, "stable": stable}
            ),
            plan_override=StageOverride(service="stable", model="plan", effort="low"),
            review_override=StageOverride(
                service="stable", model="review", effort="low"
            ),
            merge_override=StageOverride(service="stable", model="merge", effort="low"),
            improve_override=StageOverride(
                service="stable", model="improve", effort="low"
            ),
            implement_override=StageOverride(
                service="primary",
                model="primary-model",
                effort="low",
                fallback=StageOverride(
                    service="fallback",
                    model="fallback-model",
                    effort="high",
                ),
            ),
            max_iterations=2,
        )

    assert captured == ["fallback", "primary"]
    mock_sleep.assert_not_called()


def test_service_registry_resolve_snapshots_availability_per_configured_service() -> (
    None
):
    registry = ServiceRegistry(
        cast(
            dict[str, AgentService],
            {
                "codex": _SequencedAvailabilityService([False, True]),
                "claude": _FakeService(available=True),
            },
        )
    )
    override = StageOverride(
        service="codex",
        model="primary-model",
        effort="low",
        fallback=StageOverride(
            service="claude",
            model="fallback-model",
            effort="medium",
            fallback=StageOverride(
                service="codex",
                model="tertiary-model",
                effort="high",
            ),
        ),
    )

    result = registry.resolve(override, datetime.now(timezone.utc))

    assert result == StageOverride(
        service="claude",
        model="fallback-model",
        effort="medium",
        fallback=StageOverride(
            service="codex",
            model="tertiary-model",
            effort="high",
        ),
    )


def test_stage_with_no_fallback_behaves_as_before(tmp_path):
    """A stage with no fallback configured uses its primary model and effort
    regardless of other services in the registry."""
    captured: list[dict] = []

    claude_svc = _FakeService(available=True)

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
            )
        if "Implement Agent" in request.name:
            captured.append(
                {
                    "service": request.service,
                    "model": request.model,
                    "effort": request.effort,
                }
            )
            return CompletionOutput()
        return CompletionOutput()

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        service_registry=ServiceRegistry({"claude": claude_svc}),
        implement_override=StageOverride(
            service="claude", model="my-model", effort="medium"
        ),
    )

    assert len(captured) == 1
    assert captured[0]["service"] == "claude"
    assert captured[0]["model"] == "my-model"
    assert captured[0]["effort"] == "medium"


# ── AbortedHardApiError: orchestrator exits non-zero ─────────────────────────


def test_orchestrator_exits_nonzero_on_hard_api_error(tmp_path):
    """When run_iteration returns AbortedHardApiError the orchestrator must exit non-zero."""
    from agent_runtime.errors import HardAgentError

    raw_line = '{"type": "result", "is_error": true, "api_error_status": 400, "result": "Bad request"}'

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
            )
        raise HardAgentError(message=raw_line)

    with patch(
        "pycastle.iteration.auto_file_issue", return_value="https://example.com/1"
    ):
        with pytest.raises(SystemExit) as exc_info:
            _run(
                tmp_path,
                _fake_agent,
                github_service=_make_github_svc(),
                git_service=_make_git_svc(),
            )

    assert exc_info.value.code != 0


def test_orchestrator_files_upstream_bug_and_exits_on_preflight_setup_failure(
    tmp_path,
):
    """Preflight setup failures abort before diagnosis and route through the upstream bug-report path."""
    github_svc = _make_github_svc()
    runner = FakeAgentRunner(
        preflight_responses=[SetupPhaseError("preflight", "pip install failed")]
    )
    display = RecordingStatusDisplay()

    with pytest.raises(SystemExit) as exc_info:
        _run(
            tmp_path,
            agent_runner=runner,
            github_service=github_svc,
            git_service=_make_git_svc(),
            status_display=display,
            auto_file_bugs=False,
        )

    assert exc_info.value.code == 1
    assert runner.calls == []
    print_calls = [c for c in display.calls if c[0] == "print"]
    final_message = str(print_calls[-1][2])
    assert "preflight setup failed: pip install failed" in final_message
    assert "Report: https://github.com/Johannes-Kutsch/pycastle/issues/new?" in (
        final_message
    )


def test_orchestrator_exits_nonzero_on_agent_credential_failure_result(tmp_path):
    with patch(
        "pycastle.iteration.orchestrator.run_iteration",
        return_value=AbortedAgentCredentialFailure(status_code=401),
    ):
        with pytest.raises(SystemExit) as exc_info:
            _run(
                tmp_path,
                github_service=_make_github_svc(),
                git_service=_make_git_svc(),
            )

    assert exc_info.value.code == 1


def test_orchestrator_routes_missing_pyproject_declared_preflight_tool_as_setup_failure(
    tmp_path,
):
    """A missing pyproject-declared check tool is a Setup/toolchain failure, not a preflight issue."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\ndependencies = ['ruff']\n",
        encoding="utf-8",
    )
    runner = FakeAgentRunner(
        [CompletionOutput()],
        preflight_responses=[
            SetupPhaseError(
                "preflight",
                "Missing expected preflight tool 'ruff' declared in pyproject.toml.",
                command="ruff check .",
                output="Command failed (exit 127): bash: ruff: command not found",
            )
        ],
    )

    display = RecordingStatusDisplay()

    with pytest.raises(SystemExit) as exc_info:
        _run(
            tmp_path,
            agent_runner=runner,
            github_service=_make_github_svc(),
            git_service=_make_git_svc(),
            status_display=display,
            auto_file_bugs=False,
        )

    assert exc_info.value.code == 1
    assert runner.calls == []
    print_calls = [c for c in display.calls if c[0] == "print"]
    final_message = str(print_calls[-1][2])
    assert "Command: ruff check ." in final_message
    assert "Output: Command failed (exit 127): bash: ruff: command not found" in (
        final_message
    )
    report_url = next(
        line.removeprefix("Report: ")
        for line in final_message.splitlines()
        if line.startswith("Report: ")
    )
    title = parse_qs(urlparse(report_url).query)["title"][0]
    assert title == (
        "[pycastle] preflight setup failure: "
        "Missing expected preflight tool 'ruff' declared in pyproject.toml."
    )
    assert (
        "Missing expected preflight tool 'ruff' declared in pyproject.toml."
        in final_message
    )


def test_orchestrator_includes_command_and_output_in_upstream_setup_failure_report(
    tmp_path,
):
    """Handled Setup/toolchain failures must report the relevant command and output upstream."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\ndependencies = ['ruff']\n",
        encoding="utf-8",
    )
    runner = FakeAgentRunner(
        [CompletionOutput()],
        preflight_responses=[
            SetupPhaseError(
                "preflight",
                "Missing expected preflight tool 'ruff' declared in pyproject.toml.",
                command="ruff check .",
                output="Command failed (exit 127): bash: ruff: command not found",
            )
        ],
    )

    display = RecordingStatusDisplay()

    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            agent_runner=runner,
            github_service=_make_github_svc(),
            git_service=_make_git_svc(),
            status_display=display,
            auto_file_bugs=False,
        )

    print_calls = [c for c in display.calls if c[0] == "print"]
    final_message = str(print_calls[-1][2])
    report_url = next(
        line.removeprefix("Report: ")
        for line in final_message.splitlines()
        if line.startswith("Report: ")
    )
    body = parse_qs(urlparse(report_url).query)["body"][0]
    assert "Command: `ruff check .`" in body
    assert "bash: ruff: command not found" in body


def test_orchestrator_files_setup_failure_via_api_when_auto_file_bugs_is_enabled(
    tmp_path,
    monkeypatch,
):
    """Handled Setup/toolchain failures use the API filing path when upstream auto filing is enabled."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\ndependencies = ['ruff']\n",
        encoding="utf-8",
    )
    runner = FakeAgentRunner(
        [CompletionOutput()],
        preflight_responses=[
            SetupPhaseError(
                "preflight",
                "Missing expected preflight tool 'ruff' declared in pyproject.toml.",
                command="ruff check .",
                output="Command failed (exit 127): bash: ruff: command not found",
            )
        ],
    )
    display = RecordingStatusDisplay()
    monkeypatch.setenv("GH_TOKEN", "test-token")

    with patch(
        "pycastle.services.github_service.GithubService.create_issue_in",
        return_value=321,
    ) as mock_create:
        with pytest.raises(SystemExit):
            _run(
                tmp_path,
                agent_runner=runner,
                github_service=_make_github_svc(),
                git_service=_make_git_svc(),
                status_display=display,
                auto_file_bugs=True,
            )

    mock_create.assert_called_once()
    _, title, body, labels = mock_create.call_args.args
    assert title == (
        "[pycastle] preflight setup failure: "
        "Missing expected preflight tool 'ruff' declared in pyproject.toml."
    )
    assert "Command: `ruff check .`" in body
    assert "bash: ruff: command not found" in body
    assert labels == ["bug", "needs-triage"]
    print_calls = [c for c in display.calls if c[0] == "print"]
    final_message = str(print_calls[-1][2])
    assert "Report: https://github.com/Johannes-Kutsch/pycastle/issues/321" in (
        final_message
    )


def test_orchestrator_prints_setup_failure_details_locally(tmp_path):
    """Handled Setup/toolchain failures must print the failed step and captured output locally."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\ndependencies = ['ruff']\n",
        encoding="utf-8",
    )
    runner = FakeAgentRunner(
        [CompletionOutput()],
        preflight_responses=[
            SetupPhaseError(
                "preflight",
                "Missing expected preflight tool 'ruff' declared in pyproject.toml.",
                command="ruff check .",
                output="Command failed (exit 127): bash: ruff: command not found",
            )
        ],
    )
    display = RecordingStatusDisplay()

    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            agent_runner=runner,
            github_service=_make_github_svc(),
            git_service=_make_git_svc(),
            status_display=display,
            auto_file_bugs=False,
        )

    print_calls = [c for c in display.calls if c[0] == "print"]
    final_message = str(print_calls[-1][2])
    assert "preflight setup failed" in final_message
    assert "Missing expected preflight tool 'ruff'" in final_message
    assert "ruff check ." in final_message
    assert "bash: ruff: command not found" in final_message


def test_orchestrator_handles_empty_preflight_setup_failure_message(tmp_path):
    """Setup-phase aborts must stay setup-specific even when the underlying error text is empty."""
    with patch(
        "pycastle.iteration.outcome_routing.auto_file_issue",
        return_value="https://example.com/upstream/1",
    ) as mock_file:
        with pytest.raises(SystemExit) as exc_info:
            _run(
                tmp_path,
                agent_runner=FakeAgentRunner(
                    preflight_responses=[SetupPhaseError("preflight", "")]
                ),
                github_service=_make_github_svc(),
                git_service=_make_git_svc(),
            )

    assert exc_info.value.code == 1
    assert mock_file.call_args.args[0] == "[pycastle] preflight setup failure: "


# ── GithubAPIError: orchestrator exits non-zero on repo access failure ──────────


def test_orchestrator_exits_nonzero_on_github_api_error(tmp_path):
    """Repo-scoped GitHub API failures must stop the run with a clear message."""
    github_svc = _make_github_svc()
    github_svc.check_auth.return_value = "alice"
    github_svc.get_open_issues.side_effect = GithubAPIError(
        "GitHub API GET /repos/owner/repo/issues?state=open&labels=ready-for-agent&per_page=100 returned 404: Not Found",
        status=404,
        body="Not Found",
        method="GET",
        path="/repos/owner/repo/issues?state=open&labels=ready-for-agent&per_page=100",
    )
    recording = RecordingStatusDisplay()

    with pytest.raises(SystemExit) as exc_info:
        _run(
            tmp_path,
            github_service=github_svc,
            git_service=_make_git_svc(),
            status_display=recording,
        )

    assert exc_info.value.code == 1
    assert any(
        call[0] == "print"
        and "GitHub repository access failed:" in str(call[2])
        and "404" in str(call[2])
        for call in recording.calls
    )


def test_orchestrator_exits_nonzero_on_github_retry_exhaustion_without_auto_file(
    tmp_path,
):
    """Retry-exhausted GitHub requests must surface as handled terminal failures."""
    github_svc = _make_github_svc()
    github_svc.check_auth.return_value = "alice"
    github_svc.get_open_issues.side_effect = OperatorActionableGithubError(
        "GitHub API GET /repos/owner/repo/issues?state=open&labels=ready-for-agent&per_page=100 failed after 4 attempts: "
        "GitHub API GET /repos/owner/repo/issues?state=open&labels=ready-for-agent&per_page=100 returned 502: Bad Gateway",
        method="GET",
        path="/repos/owner/repo/issues?state=open&labels=ready-for-agent&per_page=100",
        attempt_count=4,
        cause=GithubAPIError(
            "GitHub API GET /repos/owner/repo/issues?state=open&labels=ready-for-agent&per_page=100 returned 502: Bad Gateway",
            status=502,
            body="Bad Gateway",
            method="GET",
            path="/repos/owner/repo/issues?state=open&labels=ready-for-agent&per_page=100",
        ),
    )
    recording = RecordingStatusDisplay()
    auto_file_calls: list[tuple] = []

    with patch(
        "pycastle.iteration.auto_file_issue",
        side_effect=lambda *args, **kwargs: auto_file_calls.append(args),
    ):
        with pytest.raises(SystemExit) as exc_info:
            _run(
                tmp_path,
                github_service=github_svc,
                git_service=_make_git_svc(),
                status_display=recording,
            )

    assert exc_info.value.code == 1
    assert auto_file_calls == []
    assert any(
        call[0] == "print"
        and "GitHub request retry limit reached:" in str(call[2])
        and "failed after 4 attempts" in str(call[2])
        for call in recording.calls
    )


# ── AbortedOperatorActionable: orchestrator files issue on consuming repo ─────


def test_orchestrator_files_issue_on_consuming_repo_when_no_existing_match(tmp_path):
    """When run_iteration returns AbortedOperatorActionable and the consuming repo has
    no matching open issue, the orchestrator files exactly one issue with the canonical
    title prefix and bug+needs-triage labels, then exits non-zero."""
    from pycastle.services import OperatorActionableGitError

    err = OperatorActionableGitError(
        "git pull failed",
        stderr="ssh: connect to host github.com port 22: Connection timed out",
        op="pull",
        attempt_count=4,
    )
    git_svc = _make_git_svc()
    git_svc.pull_with_merge_fallback.side_effect = err
    git_svc.get_github_remote_repo.return_value = ("consuming-owner", "consuming-repo")

    github_svc = _make_github_svc()
    github_svc.repo = "consuming-owner/consuming-repo"
    github_svc.search_open_issues_by_title.return_value = []
    github_svc.create_issue_in.return_value = 99

    with pytest.raises(SystemExit) as exc_info:
        _run(tmp_path, git_service=git_svc, github_service=github_svc)

    assert exc_info.value.code != 0
    github_svc.create_issue_in.assert_called_once()
    call_args = github_svc.create_issue_in.call_args
    owner_repo, title, body, labels = call_args[0]
    assert owner_repo == "consuming-owner/consuming-repo"
    assert title.startswith("[pycastle] git remote unreachable")
    assert labels == ["bug", "needs-triage"]


def test_orchestrator_skips_filing_when_matching_open_issue_exists(tmp_path):
    """When the consuming repo already has an open issue matching the title prefix,
    no new issue is filed, but the orchestrator still exits non-zero."""
    from pycastle.services import OperatorActionableGitError

    err = OperatorActionableGitError(
        "git pull failed",
        stderr="remote: Repository not found",
        op="pull",
        attempt_count=1,
    )
    git_svc = _make_git_svc()
    git_svc.pull_with_merge_fallback.side_effect = err
    git_svc.get_github_remote_repo.return_value = ("consuming-owner", "consuming-repo")

    github_svc = _make_github_svc()
    github_svc.repo = "consuming-owner/consuming-repo"
    github_svc.search_open_issues_by_title.return_value = [42]

    with pytest.raises(SystemExit) as exc_info:
        _run(tmp_path, git_service=git_svc, github_service=github_svc)

    assert exc_info.value.code != 0
    github_svc.create_issue_in.assert_not_called()


def test_orchestrator_filed_issue_body_contains_diagnostic_info(tmp_path):
    """The filed issue body must contain stderr, attempt count, op name, and host/version info."""
    from pycastle.services import OperatorActionableGitError

    err = OperatorActionableGitError(
        "git pull failed",
        stderr="ssh: connect to host github.com port 22: Connection timed out",
        op="pull",
        attempt_count=4,
    )
    git_svc = _make_git_svc()
    git_svc.pull_with_merge_fallback.side_effect = err

    github_svc = _make_github_svc()
    github_svc.repo = "consuming-owner/consuming-repo"
    github_svc.search_open_issues_by_title.return_value = []
    github_svc.create_issue_in.return_value = 99

    with pytest.raises(SystemExit):
        _run(tmp_path, git_service=git_svc, github_service=github_svc)

    call_args = github_svc.create_issue_in.call_args
    _owner_repo, _title, body, _labels = call_args[0]
    assert "Connection timed out" in body
    assert "4" in body
    assert "pull" in body
    assert "pycastle" in body.lower() or "Python" in body or "OS" in body


def test_orchestrator_operator_actionable_never_routes_to_pycastle_upstream(tmp_path):
    """OperatorActionableGitError must bypass auto_file_issue; pycastle's bug_report_repo
    must receive nothing."""
    from pycastle.services import OperatorActionableGitError

    err = OperatorActionableGitError(
        "git pull failed",
        stderr="remote: Repository not found",
        op="pull",
        attempt_count=1,
    )
    git_svc = _make_git_svc()
    git_svc.pull_with_merge_fallback.side_effect = err

    github_svc = _make_github_svc()
    github_svc.repo = "consuming-owner/consuming-repo"
    github_svc.search_open_issues_by_title.return_value = []
    github_svc.create_issue_in.return_value = 5

    auto_file_calls: list = []

    with patch(
        "pycastle.iteration.auto_file_issue",
        side_effect=lambda *a, **kw: auto_file_calls.append(a),
    ):
        with pytest.raises(SystemExit):
            _run(tmp_path, git_service=git_svc, github_service=github_svc)

    assert auto_file_calls == [], (
        "auto_file_issue must not be called for operator-actionable git errors"
    )


# ── Issue-901: log maintenance wired into pycastle run ───────────────────────


def test_log_maintenance_trims_oversized_log_after_successful_run(tmp_path):
    """After a successful run, oversized log files must be trimmed to 10,000 lines."""
    logs_dir = tmp_path / "pycastle" / "logs"
    logs_dir.mkdir(parents=True)
    cron_log = logs_dir / "cron.log"
    cron_log.write_text("\n".join(str(i) for i in range(11_000)))

    async def _fake_run_agent(request: RunRequest):
        return _plan_output([])

    mock_github = _make_github_svc()
    mock_github.get_open_issues.return_value = []

    _run(tmp_path, _fake_run_agent, github_service=mock_github, logs_dir=logs_dir)

    lines = cron_log.read_text().splitlines()
    assert len(lines) == 10_000, f"Expected 10,000 lines after trim; got {len(lines)}"


def test_log_maintenance_runs_after_error_exit(tmp_path):
    """Log maintenance must run even when the orchestrator exits with an error."""
    logs_dir = tmp_path / "pycastle" / "logs"
    logs_dir.mkdir(parents=True)
    cron_log = logs_dir / "cron.log"
    cron_log.write_text("\n".join(str(i) for i in range(11_000)))

    async def _fake_run_agent(request: RunRequest):
        if "Pre-Flight Reporter" in request.name:
            return IssueOutput(number=99, labels=["ready-for-human"])
        return CompletionOutput()

    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            agent_runner=FakeAgentRunner(
                side_effect=_fake_run_agent,
                preflight_responses=[
                    [_preflight_failure("ruff", "ruff check .", "E501")]
                ],
            ),
            github_service=_make_github_svc_hitl(),
            logs_dir=logs_dir,
        )

    lines = cron_log.read_text().splitlines()
    assert len(lines) == 10_000, (
        f"Log maintenance must run even on error exit; got {len(lines)} lines"
    )


def test_log_maintenance_deletes_old_log_files_after_run(tmp_path):
    """After a run, log files older than 30 days must be deleted."""
    import os
    import time

    logs_dir = tmp_path / "pycastle" / "logs"
    logs_dir.mkdir(parents=True)
    old_log = logs_dir / "old.log"
    old_log.write_text("stale data")
    stale_time = time.time() - 31 * 24 * 3600
    os.utime(old_log, (stale_time, stale_time))

    async def _fake_run_agent(request: RunRequest):
        return _plan_output([])

    mock_github = _make_github_svc()
    mock_github.get_open_issues.return_value = []

    _run(tmp_path, _fake_run_agent, github_service=mock_github, logs_dir=logs_dir)

    assert not old_log.exists(), "Old log files must be deleted after run"


# ── Issue 1940: merge close failure orchestrator handling ─────────────────────


def test_merge_close_failure_prints_issue_numbers_and_stops_run(tmp_path):
    """When merge phase files close-failure issues, the orchestrator prints them and stops without another iteration."""
    issues = [
        {
            "number": 1,
            "title": "Fail close",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]

    async def _fake_run_agent(request: RunRequest):
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return _plan_output(issues)

    mock_github = _make_github_svc(numbers=[1])

    def _raise_on_close(number):
        if number == 1:
            raise RuntimeError("close failed")

    mock_github.close_issue_with_parents.side_effect = _raise_on_close
    recording = RecordingStatusDisplay()

    git_svc = _make_git_svc()
    git_svc.try_merge.return_value = True

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=git_svc,
        github_service=mock_github,
        status_display=recording,
        max_iterations=2,
    )

    iteration_headers = [
        c for c in recording.calls if c[0] == "print" and "=== Iteration" in str(c[2])
    ]
    assert len(iteration_headers) == 1, "Run must stop after the first iteration"
    close_failure_prints = [
        c
        for c in recording.calls
        if c[0] == "print" and "issue close failed" in str(c[2]).lower()
    ]
    assert close_failure_prints, "Orchestrator must print a merge close failure message"
    assert any("#999" in str(c[2]) for c in close_failure_prints)


def test_run_iteration_returns_merge_close_failure_on_close_error(tmp_path):
    """run_iteration must return MergeCloseFailure when merge phase collects close-failure issue numbers."""
    from pycastle.iteration import MergeCloseFailure, run_iteration

    mock_github = _make_github_svc(numbers=[1])

    def _raise_on_close(number):
        if number == 1:
            raise RuntimeError("close failed")

    mock_github.close_issue_with_parents.side_effect = _raise_on_close

    async def _fake_run_agent(request: RunRequest):
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return _plan_output(
            [{"number": 1, "title": "Fail close", "body": "x" * 100, "comments": []}]
        )

    git_svc = _make_git_svc()
    git_svc.try_merge.return_value = True

    deps = _make_deps(
        tmp_path,
        _fake_run_agent,
        git_svc=git_svc,
        github_svc=mock_github,
    )
    outcome = asyncio.run(run_iteration(deps))

    assert isinstance(outcome, MergeCloseFailure)
    assert outcome.filed_issue_numbers == [999]


def test_run_iteration_returns_continue_when_all_issues_close(tmp_path):
    """run_iteration must return Continue when merge phase closes all issues successfully."""
    from pycastle.iteration import Continue, run_iteration

    async def _fake_run_agent(request: RunRequest):
        if "Implement Agent" in request.name:
            return CompletionOutput()
        return _plan_output(
            [{"number": 1, "title": "Ok", "body": "x" * 100, "comments": []}]
        )

    git_svc = _make_git_svc()
    git_svc.try_merge.return_value = True

    deps = _make_deps(
        tmp_path,
        _fake_run_agent,
        git_svc=git_svc,
        github_svc=_make_github_svc(numbers=[1]),
    )
    outcome = asyncio.run(run_iteration(deps))

    assert isinstance(outcome, Continue)


# ── AbortedModelNotAvailable: orchestrator routing ────────────────────────────


def test_model_not_available_with_available_candidate_does_not_sleep(tmp_path):
    """When AbortedModelNotAvailable is received and the service registry still has an
    available candidate, the orchestrator must continue immediately with no sleep."""
    mock_github = _make_github_svc()
    mock_github.get_open_issues.side_effect = [
        [
            {
                "number": 1,
                "title": "Default Issue",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            }
        ],
        [],
    ]

    svc = _FakeService(available=True)

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
            )
        raise ModelNotAvailableError(service="claude", model="claude-opus-4-5")

    with patch("time.sleep") as mock_sleep:
        _run(
            tmp_path,
            _fake_run_agent,
            github_service=mock_github,
            service_registry=ServiceRegistry({"claude": svc}),
            max_iterations=2,
        )

    mock_sleep.assert_not_called()


def test_model_not_available_with_temporarily_exhausted_chain_sleeps(tmp_path):
    """When AbortedModelNotAvailable is received and all chain candidates are temporarily
    exhausted (finite wake time), the orchestrator must sleep until that wake time."""
    mock_github = _make_github_svc()
    mock_github.get_open_issues.side_effect = [
        [
            {
                "number": 1,
                "title": "Default Issue",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            }
        ],
        [],
    ]

    wake_time = datetime.now(timezone.utc) + timedelta(hours=1)
    svc = _FakeService(available=False, wake_time=wake_time)

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
            )
        raise ModelNotAvailableError(service="claude", model="claude-opus-4-5")

    with patch("time.sleep") as mock_sleep:
        _run(
            tmp_path,
            _fake_run_agent,
            github_service=mock_github,
            service_registry=ServiceRegistry({"claude": svc}),
            max_iterations=2,
        )

    mock_sleep.assert_called_once()
    assert mock_sleep.call_args[0][0] > 0


def test_model_not_available_with_no_finite_wake_time_stops(tmp_path):
    """When AbortedModelNotAvailable is received and every chain candidate has no finite
    wake time (all permanently restricted or exhausted), the orchestrator must stop with
    a clear message and not sleep."""
    mock_github = _make_github_svc()
    mock_github.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Default Issue",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]

    svc = _FakeService(available=False, wake_time=None)

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
            )
        raise ModelNotAvailableError(service="claude", model="claude-opus-4-5")

    recording = RecordingStatusDisplay()
    with patch("time.sleep") as mock_sleep:
        _run(
            tmp_path,
            _fake_run_agent,
            github_service=mock_github,
            service_registry=ServiceRegistry({"claude": svc}),
            max_iterations=1,
            status_display=recording,
        )

    mock_sleep.assert_not_called()
    msgs = [str(c[2]) for c in recording.calls if c[0] == "print"]
    assert any("not available" in m.lower() or "model" in m.lower() for m in msgs), (
        f"Expected a message mentioning model unavailability; got: {msgs}"
    )


def test_model_not_available_stage_scoped_routing_uses_chain_not_global_availability(
    tmp_path,
):
    """When a stage's model is restricted but the service is globally available (other
    models work), the orchestrator must stop rather than continue.

    Without stage_key on ModelNotAvailableError, the orchestrator calls
    has_available(now) — which ignores model restrictions — and incorrectly continues
    to the next iteration. With stage_key set, it calls has_available_for(stage_override,
    now), which sees that the implement chain's specific model is restricted and stops."""
    mock_github = _make_github_svc()
    mock_github.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Default Issue",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]

    class _ModelRestrictedService(_FakeService):
        """Globally available service whose 'sonnet' model is permanently restricted."""

        def __init__(self, restricted_model: str) -> None:
            super().__init__(available=True)
            self._restricted_model = restricted_model

        def is_available(self, now=None, *, model=None) -> bool:
            if model is not None and model == self._restricted_model:
                return False
            return True

        def next_wake_time(self):
            return None

    implement_calls = [0]

    async def _fake_run_agent(request: RunRequest):
        if "Implement Agent" in request.name:
            implement_calls[0] += 1
            # Raise with stage_key as the runner would, so stage-scoped routing fires.
            raise ModelNotAvailableError(
                service="claude", model="sonnet", stage_key="implement"
            )
        return _plan_output(
            [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
        )

    recording = RecordingStatusDisplay()
    with patch("time.sleep") as mock_sleep:
        _run(
            tmp_path,
            _fake_run_agent,
            github_service=mock_github,
            service_registry=ServiceRegistry(
                {"claude": _ModelRestrictedService("sonnet")}
            ),
            max_iterations=3,
            status_display=recording,
        )

    # Stage-scoped routing: has_available_for(implement_override, now) sees sonnet is
    # restricted → Stop after the first implement attempt, not Continue (which would
    # loop max_iterations=3 times via global has_available=True).
    mock_sleep.assert_not_called()
    assert implement_calls[0] == 1, (
        f"Expected 1 implement attempt (stop on restricted model), got {implement_calls[0]}"
    )
    msgs = [str(c[2]) for c in recording.calls if c[0] == "print"]
    assert any("not available" in m.lower() for m in msgs), (
        f"Expected a stop message mentioning unavailability; got: {msgs}"
    )


def test_model_not_available_sleep_message_does_not_say_usage_limit(tmp_path):
    """When the run sleeps after AbortedModelNotAvailable, the status message must not
    say 'usage limit reached' — that phrase belongs to credential exhaustion, not model
    restriction."""
    mock_github = _make_github_svc()
    mock_github.get_open_issues.side_effect = [
        [
            {
                "number": 1,
                "title": "Default Issue",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            }
        ],
        [],
    ]

    wake_time = datetime.now(timezone.utc) + timedelta(hours=1)
    svc = _FakeService(available=False, wake_time=wake_time)

    async def _fake_run_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix", "body": "x" * 100, "comments": []}]
            )
        raise ModelNotAvailableError(
            service="claude", model="sonnet", stage_key="implement"
        )

    recording = RecordingStatusDisplay()
    with patch("time.sleep"):
        _run(
            tmp_path,
            _fake_run_agent,
            github_service=mock_github,
            service_registry=ServiceRegistry({"claude": svc}),
            max_iterations=2,
            status_display=recording,
        )

    msgs = [str(c[2]) for c in recording.calls if c[0] == "print"]
    sleep_msgs = [m for m in msgs if "sleeping until" in m.lower()]
    assert sleep_msgs, f"Expected at least one sleep message; got: {msgs}"
    assert all("usage limit" not in m.lower() for m in sleep_msgs), (
        f"Sleep message incorrectly says 'usage limit reached': {sleep_msgs}"
    )
