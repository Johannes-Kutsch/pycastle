"""Tests for PreflightCache.get_safe_sha: observable behaviour via the public interface."""

import asyncio
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agent_runtime.errors import AgentCredentialFailureError

from pycastle.agents.output_protocol import AgentRole, CompletionOutput, IssueOutput
from pycastle.config import Config, StageOverride
from pycastle.display.status_display import PlainStatusDisplay
from pycastle.errors import DockerError, SetupPhaseError
from pycastle.infrastructure.preflight_failure_interpreter import (
    PreflightCommandFailure,
)
from pycastle.infrastructure.worktree import (
    SandboxWorktreeIntent,
    reusable_sandbox_worktree_identity,
    worktree_identity,
)
from pycastle.iteration.preflight import (
    PreflightAFK,
    PreflightCache,
    PreflightHITL,
    PreflightReady,
)
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.services import (
    GitCommandError,
    GithubService,
    GitService,
    ServiceRegistry,
)
from pycastle.services._operating_branch_refresh import OperatingBranchDiverged
from pycastle.services.runtime_services import AgentService
from pycastle.session import RoleSession
from tests.support import (
    FakeAgentRunner,
    RecordingStatusDisplay,
    _make_deps,
    functional_git_svc,
)


def _diverge_fingerprint(safe_sha: str, branch: str) -> str:
    return hashlib.sha256(f"{safe_sha}\n{branch}".encode()).hexdigest()


class _FixedSandboxWorktree:
    """Async context manager that yields a fixed path without touching git."""

    def __init__(self, path):
        self._path = path

    async def __aenter__(self):
        return self._path

    async def __aexit__(self, *args):
        pass


def _conflict_git_svc(sha: str, branch: str) -> MagicMock:
    svc = MagicMock(spec=GitService)
    svc.is_working_tree_clean.return_value = True
    svc.get_head_sha.return_value = sha
    svc.get_branch_sha.return_value = sha
    svc.refresh_operating_branch.return_value = OperatingBranchDiverged()
    return svc


@pytest.fixture
def git_svc():
    svc = functional_git_svc()
    svc.get_head_sha.return_value = "abc123"
    svc.get_branch_sha.return_value = "abc123"
    svc.is_working_tree_clean.return_value = True
    return svc


@pytest.fixture
def github_svc():
    return MagicMock(spec=GithubService)


def _preflight_failure(
    check_name: str, command: str, output: str
) -> PreflightCommandFailure:
    return PreflightCommandFailure(
        check_name=check_name,
        command=command,
        output=output,
    )


# ── get_safe_sha: operating branch SHA ────────────────────────────────────────


def test_get_safe_sha_resolves_sha_from_operating_branch_not_repo_root(
    tmp_path, github_svc
):
    """Safe SHA must come from the operating branch ref, not repo root HEAD."""
    git_svc = functional_git_svc()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_branch_sha.return_value = "branch-tip-sha"
    git_svc.get_head_sha.return_value = "root-head-sha"  # different from branch SHA

    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightReady)
    assert result.sha == "branch-tip-sha"


# ── get_safe_sha: basic return variants ──────────────────────────────────────


def test_get_safe_sha_returns_ready_with_sha_when_checks_pass(
    tmp_path, git_svc, github_svc
):
    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightReady)
    assert result.sha == "abc123"


def test_get_safe_sha_returns_hitl_when_checks_fail_with_hitl_label(
    tmp_path, git_svc, github_svc
):
    fake = FakeAgentRunner(
        [IssueOutput(number=55, labels=["bug", "ready-for-human"])],
        preflight_responses=[[_preflight_failure("ruff", "ruff check .", "E501")]],
    )
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightHITL)
    assert result.issue_number == 55
    assert result.sha == "abc123"


def test_get_safe_sha_preflight_issue_uses_preflight_issue_override_service(
    tmp_path, git_svc, github_svc
):
    fake = FakeAgentRunner(
        [IssueOutput(number=55, labels=["bug", "ready-for-human"])],
        preflight_responses=[[_preflight_failure("ruff", "ruff check .", "E501")]],
    )
    deps = _make_deps(
        tmp_path,
        fake,
        git_svc=git_svc,
        github_svc=github_svc,
        cfg=Config(
            max_parallel=4,
            max_iterations=1,
            preflight_issue_override=StageOverride(service="codex", effort="medium"),
        ),
    )
    cache = PreflightCache()

    result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightHITL)
    assert fake.calls[0].service == "codex"


def test_get_safe_sha_preflight_issue_resolves_override_at_failure_dispatch(
    tmp_path, git_svc, github_svc
):
    fake = FakeAgentRunner(
        [IssueOutput(number=55, labels=["bug", "ready-for-human"])],
        preflight_responses=[[_preflight_failure("ruff", "ruff check .", "E501")]],
    )
    unavailable = MagicMock(spec=AgentService)
    unavailable.is_available.return_value = False
    available = MagicMock(spec=AgentService)
    available.is_available.return_value = True
    deps = _make_deps(
        tmp_path,
        fake,
        git_svc=git_svc,
        github_svc=github_svc,
        cfg=Config(
            max_parallel=4,
            max_iterations=1,
            preflight_issue_override=StageOverride(
                service="claude",
                model="opus",
                effort="high",
                fallback=StageOverride(
                    service="codex",
                    model="gpt-5.5",
                    effort="medium",
                ),
            ),
        ),
        service_registry=ServiceRegistry({"claude": unavailable, "codex": available}),
    )
    cache = PreflightCache()

    result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightHITL)
    assert fake.calls[0].service == "codex"
    assert fake.calls[0].model == "gpt-5.5"
    assert fake.calls[0].effort == "medium"


def test_get_safe_sha_returns_afk_when_checks_fail_with_afk_label(
    tmp_path, git_svc, github_svc
):
    fake = FakeAgentRunner(
        [IssueOutput(number=42, labels=["bug", "ready-for-agent", "behavior-slice"])],
        preflight_responses=[[_preflight_failure("ruff", "ruff check .", "E501")]],
    )
    github_svc.get_issue.return_value = {
        "number": 42,
        "body": "x" * 100,
        "labels": ["behavior-slice"],
    }
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightAFK)
    assert result.issue_number == 42
    assert result.sha == "abc123"


def test_get_safe_sha_files_fallback_issue_when_preflight_reporter_mount_is_invalid(
    tmp_path, git_svc, github_svc
):
    (tmp_path / "pycastle" / ".worktrees").mkdir(parents=True, exist_ok=True)
    invalid_mount = tmp_path / "outside-worktrees" / "preflight-sandbox"
    invalid_mount.mkdir(parents=True, exist_ok=True)

    fake = FakeAgentRunner(
        [],
        preflight_responses=[[_preflight_failure("ruff", "ruff check .", "E501")]],
    )
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []
    github_svc.create_issue_in.return_value = (654, 10654)
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    class _InvalidTransientWorktree:
        async def __aenter__(self):
            return invalid_mount

        async def __aexit__(self, exc_type, exc, tb):
            return None

    with patch(
        "pycastle.infrastructure.worktree.detached_transient_worktree",
        return_value=_InvalidTransientWorktree(),
    ):
        result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightHITL)
    assert result.issue_number == 654
    assert len(fake.preflight_calls) == 1
    assert fake.calls == []
    github_svc.create_issue_in.assert_called_once()
    repo, title, body, labels = github_svc.create_issue_in.call_args.args
    assert repo == github_svc.repo
    assert "Pre-Flight Reporter" in title
    assert labels == ["bug", "needs-triage"]
    assert "No diagnostic agent ran." in body
    assert "Role: preflight_issue" in body
    assert (
        f"Expected mount path: {tmp_path / 'pycastle' / '.worktrees' / 'preflight-sandbox'}"
        in body
    )
    assert "Reason: invalid_mount_path" in body
    assert "Preflight check 'ruff' failed while running 'ruff check .'" in body


def test_get_safe_sha_routes_requirements_declared_missing_tool_to_setup_failure(
    tmp_path, git_svc, github_svc
):
    (tmp_path / "requirements.txt").write_text("ruff==0.6.9\n", encoding="utf-8")
    fake = FakeAgentRunner(
        [],
        preflight_responses=[
            [
                _preflight_failure(
                    "ruff",
                    "ruff check .",
                    "Command failed (exit 127): bash: ruff: command not found",
                )
            ]
        ],
    )
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    with pytest.raises(SetupPhaseError) as exc_info:
        asyncio.run(cache.get_safe_sha(deps))

    err = exc_info.value
    assert (
        str(err)
        == "Missing expected preflight tool 'ruff' declared in requirements.txt."
    )
    assert err.command == "ruff check ."
    assert err.output == "Command failed (exit 127): bash: ruff: command not found"
    assert fake.calls == []


def test_get_safe_sha_preserves_first_ordinary_declared_tool_failure_routing_when_later_failure_is_missing_declared_tool(
    tmp_path, git_svc, github_svc
):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\ndependencies = ['pytest>=8.0', 'mypy>=1.0']\n",
        encoding="utf-8",
    )
    fake = FakeAgentRunner(
        [IssueOutput(number=55, labels=["bug", "ready-for-agent", "behavior-slice"])],
        preflight_responses=[
            [
                _preflight_failure(
                    "pytest", "pytest", "FAILED tests/test_demo.py::test_it"
                ),
                _preflight_failure(
                    "mypy",
                    "mypy .",
                    "Command failed (exit 127): bash: mypy: command not found",
                ),
            ]
        ],
    )
    github_svc.get_issue.return_value = {
        "number": 55,
        "body": "x" * 100,
        "labels": ["behavior-slice"],
    }
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightAFK)
    assert result.issue_number == 55
    assert len(fake.preflight_calls) == 1
    assert len(fake.calls) == 1


def test_get_safe_sha_preserves_hitl_routing_for_first_ordinary_declared_tool_failure_when_later_failure_is_missing_declared_tool(
    tmp_path, git_svc, github_svc
):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\ndependencies = ['pytest>=8.0', 'mypy>=1.0']\n",
        encoding="utf-8",
    )
    fake = FakeAgentRunner(
        [IssueOutput(number=56, labels=["bug", "ready-for-human"])],
        preflight_responses=[
            [
                _preflight_failure(
                    "pytest", "pytest", "FAILED tests/test_demo.py::test_it"
                ),
                _preflight_failure(
                    "mypy",
                    "mypy .",
                    "Command failed (exit 127): bash: mypy: command not found",
                ),
            ]
        ],
    )
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightHITL)
    assert result.issue_number == 56
    assert len(fake.preflight_calls) == 1
    assert len(fake.calls) == 1


def test_get_safe_sha_routes_declared_missing_tool_with_shell_not_found_output_to_setup_failure(
    tmp_path, git_svc, github_svc
):
    (tmp_path / "requirements.txt").write_text("ruff==0.6.9\n", encoding="utf-8")
    fake = FakeAgentRunner(
        [IssueOutput(number=55, labels=["bug", "ready-for-human"])],
        preflight_responses=[
            [
                _preflight_failure(
                    "ruff",
                    "ruff check .",
                    "Command failed (exit 127): /bin/sh: 1: ruff: not found",
                )
            ]
        ],
    )
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    with pytest.raises(SetupPhaseError) as exc_info:
        asyncio.run(cache.get_safe_sha(deps))

    err = exc_info.value
    assert (
        str(err)
        == "Missing expected preflight tool 'ruff' declared in requirements.txt."
    )
    assert err.command == "ruff check ."
    assert err.output == "Command failed (exit 127): /bin/sh: 1: ruff: not found"
    assert fake.calls == []


def test_get_safe_sha_does_not_cache_verdict_when_missing_declared_tool_raises_setup_failure(
    tmp_path, git_svc, github_svc
):
    (tmp_path / "requirements.txt").write_text("ruff==0.6.9\n", encoding="utf-8")
    fake = FakeAgentRunner(
        [],
        preflight_responses=[
            [
                _preflight_failure(
                    "ruff",
                    "ruff check .",
                    "Command failed (exit 127): /bin/sh: 1: ruff: not found",
                )
            ],
            [],
        ],
    )
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    with pytest.raises(SetupPhaseError):
        asyncio.run(cache.get_safe_sha(deps))

    result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightReady)
    assert result.sha == "abc123"
    assert len(fake.preflight_calls) == 2
    assert fake.calls == []


def test_get_safe_sha_propagates_setup_phase_error_metadata_unchanged(
    tmp_path, git_svc, github_svc
):
    err = SetupPhaseError(
        "setup",
        "pip install failed",
        command="pip install -e '.[dev]'",
        output="No matching distribution found",
    )
    fake = FakeAgentRunner([], preflight_responses=[err])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    with pytest.raises(SetupPhaseError) as exc_info:
        asyncio.run(cache.get_safe_sha(deps))

    assert exc_info.value is err
    assert exc_info.value.phase == "setup"
    assert exc_info.value.command == "pip install -e '.[dev]'"
    assert exc_info.value.output == "No matching distribution found"
    assert fake.calls == []


def test_get_safe_sha_treats_runner_failure_fact_as_ordinary_pre_flight_failure(
    tmp_path, git_svc, github_svc
):
    fake = FakeAgentRunner(
        [IssueOutput(number=55, labels=["bug", "ready-for-human"])],
        preflight_responses=[
            [
                _preflight_failure(
                    "ruff",
                    "ruff check .",
                    "Command failed (exit 127): bash: ruff: command not found",
                )
            ]
        ],
    )
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightHITL)
    assert result.issue_number == 55
    assert len(fake.preflight_calls) == 1
    assert len(fake.calls) == 1


def test_get_safe_sha_keeps_declared_source_quality_failure_on_preflight_issue_route(
    tmp_path, git_svc, github_svc
):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\ndependencies = ['ruff>=0.5']\n",
        encoding="utf-8",
    )
    fake = FakeAgentRunner(
        [IssueOutput(number=56, labels=["bug", "ready-for-human"])],
        preflight_responses=[
            [
                _preflight_failure(
                    "ruff",
                    "ruff check .",
                    "src/demo.py:1:1: F401 `os` imported but unused",
                )
            ]
        ],
    )
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightHITL)
    assert result.issue_number == 56
    assert len(fake.preflight_calls) == 1
    assert len(fake.calls) == 1


def test_get_safe_sha_preserves_original_first_failure_details_after_analysis_and_caches_afk_verdict(
    tmp_path, git_svc, github_svc
):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\ndependencies = ['ruff>=0.5']\n",
        encoding="utf-8",
    )
    fake = FakeAgentRunner(
        [IssueOutput(number=77, labels=["bug", "ready-for-agent", "behavior-slice"])],
        preflight_responses=[
            [
                _preflight_failure(
                    "lint",
                    "python -X dev -m ruff check .",
                    "src/demo.py:1:1: F401 `os` imported but unused",
                )
            ]
        ],
    )
    github_svc.get_issue.return_value = {
        "number": 77,
        "body": "x" * 100,
        "labels": ["behavior-slice"],
    }
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result1 = asyncio.run(cache.get_safe_sha(deps))
    result2 = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result1, PreflightAFK)
    assert result1.issue_number == 77
    assert result2 is result1
    assert len(fake.preflight_calls) == 1
    assert len(fake.calls) == 1


def test_get_safe_sha_routes_first_ordinary_preflight_failure_decision_through_preflight_issue_scope(
    tmp_path, git_svc, github_svc
):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\ndependencies = ['ruff>=0.5', 'mypy>=1.0']\n",
        encoding="utf-8",
    )
    fake = FakeAgentRunner(
        [IssueOutput(number=55, labels=["bug", "ready-for-human"])],
        preflight_responses=[
            [
                _preflight_failure(
                    "lint",
                    "python -X dev -m ruff check .",
                    "src/demo.py:1:1: F401 `os` imported but unused",
                ),
                _preflight_failure(
                    "mypy",
                    "mypy .",
                    "Command failed (exit 127): bash: mypy: command not found",
                ),
            ]
        ],
    )
    deps = _make_deps(
        tmp_path,
        fake,
        git_svc=git_svc,
        github_svc=github_svc,
        cfg=Config(
            preflight_issue_override=StageOverride(service="codex", effort="medium")
        ),
    )
    cache = PreflightCache()

    result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightHITL)
    assert fake.calls[0].prompt.scope_args == {
        "CHECK_NAME": "lint",
        "COMMAND": "python -X dev -m ruff check .",
        "OUTPUT": "src/demo.py:1:1: F401 `os` imported but unused",
    }
    assert len(fake.preflight_calls) == 1
    assert len(fake.calls) == 1


# ── get_safe_sha: same-SHA cache hit ─────────────────────────────────────────


def test_get_safe_sha_returns_cached_verdict_on_same_sha_second_call(
    tmp_path, git_svc, github_svc
):
    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result1 = asyncio.run(cache.get_safe_sha(deps))
    result2 = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result1, PreflightReady)
    assert result2 is result1
    assert len(fake.preflight_calls) == 1


def test_get_safe_sha_failure_cached_on_second_call_at_same_sha(
    tmp_path, git_svc, github_svc
):
    """Cache miss + checks fail dispatches preflight-issue once; second call at same
    SHA reuses the cached AFK verdict without re-running checks or re-filing."""
    fake = FakeAgentRunner(
        [IssueOutput(number=99, labels=["ready-for-agent", "refactor-slice"])],
        preflight_responses=[[_preflight_failure("mypy", "mypy .", "error")]],
    )
    github_svc.get_issue.return_value = {
        "number": 99,
        "body": "x" * 100,
        "labels": ["refactor-slice"],
    }
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result1 = asyncio.run(cache.get_safe_sha(deps))
    result2 = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result1, PreflightAFK)
    assert result1.issue_number == 99
    assert result2 is result1
    assert len(fake.preflight_calls) == 1
    assert len(fake.calls) == 1  # preflight-issue agent called only once


# ── get_safe_sha: HEAD advance replaces slot ──────────────────────────────────


def test_get_safe_sha_reruns_checks_when_head_advances(tmp_path, git_svc, github_svc):
    git_svc.get_branch_sha.side_effect = ["sha-v1", "sha-v2"]
    fake = FakeAgentRunner([], preflight_responses=[[], []])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result1 = asyncio.run(cache.get_safe_sha(deps))
    result2 = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result1, PreflightReady)
    assert result1.sha == "sha-v1"
    assert isinstance(result2, PreflightReady)
    assert result2.sha == "sha-v2"
    assert len(fake.preflight_calls) == 2


# ── get_safe_sha: preflight pull via fetch ────────────────────────────────────


def test_get_safe_sha_uses_refresh_not_pull_on_repo_root(tmp_path, git_svc, github_svc):
    """Preflight must call refresh_operating_branch, not pull_with_merge_fallback at repo root."""
    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    asyncio.run(cache.get_safe_sha(deps))

    git_svc.refresh_operating_branch.assert_called_once_with(
        tmp_path, deps.cfg.operating_branch
    )
    git_svc.pull_with_merge_fallback.assert_not_called()


# ── get_safe_sha: pull failure ────────────────────────────────────────────────


def test_get_safe_sha_propagates_git_command_error_on_refresh_failure(
    tmp_path, git_svc, github_svc
):
    git_svc.refresh_operating_branch.side_effect = GitCommandError(
        "git fetch origin failed"
    )
    fake = FakeAgentRunner([], preflight_responses=[])
    deps = _make_deps(
        tmp_path,
        fake,
        git_svc=git_svc,
        github_svc=github_svc,
        status_display=PlainStatusDisplay(),
    )
    cache = PreflightCache()

    with pytest.raises(GitCommandError):
        asyncio.run(cache.get_safe_sha(deps))


def test_get_safe_sha_leaves_slot_unchanged_on_pull_failure(
    tmp_path, git_svc, github_svc
):
    """Fetch failure must not corrupt the cache slot."""
    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    # First call succeeds
    result1 = asyncio.run(cache.get_safe_sha(deps))
    assert isinstance(result1, PreflightReady)

    # Second call: refresh fails
    git_svc.refresh_operating_branch.side_effect = GitCommandError("fetch failed")
    # Different SHA so it would invalidate cache
    git_svc.get_branch_sha.return_value = "sha-new"

    with pytest.raises(GitCommandError):
        asyncio.run(cache.get_safe_sha(deps))

    # The slot still holds the original verdict
    git_svc.refresh_operating_branch.side_effect = None
    git_svc.get_branch_sha.return_value = "abc123"
    result3 = asyncio.run(cache.get_safe_sha(deps))
    assert result3 is result1


# ── get_safe_sha: operating-branch checkout gate ──────────────────────────────


def test_get_safe_sha_proceeds_when_working_tree_dirty_but_operating_branch_not_checked_out(
    tmp_path, github_svc
):
    """A dirty repo root must not block preflight when the operating branch is not checked out."""
    git_svc = functional_git_svc()
    git_svc.get_branch_sha.return_value = "abc123"
    git_svc.is_working_tree_clean.return_value = False
    git_svc.list_worktrees_with_branches.return_value = []

    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightReady)


def test_get_safe_sha_waits_when_operating_branch_checked_out_in_worktree(
    tmp_path, github_svc
):
    """When the operating branch is checked out in a worktree, preflight waits and reports phase+path."""
    checkout_path = tmp_path / "some-worktree"
    git_svc = functional_git_svc()
    git_svc.get_branch_sha.return_value = "abc123"
    git_svc.list_worktrees_with_branches.side_effect = [
        [(checkout_path, "main")],
        [],
    ]

    display = RecordingStatusDisplay()
    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, status_display=display
    )
    cache = PreflightCache()

    with patch("pycastle.iteration._utils.asyncio.sleep", new_callable=AsyncMock):
        result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightReady)
    printed = [call for call in display.calls if call[0] == "print"]
    assert any(
        "Preflight" in str(call) and str(checkout_path) in str(call) for call in printed
    )


def test_get_safe_sha_clean_checkout_of_operating_branch_blocks_preflight(
    tmp_path, github_svc
):
    """A clean checkout of the operating branch blocks preflight — cleanliness is irrelevant."""
    checkout_path = tmp_path / "clean-worktree"
    git_svc = functional_git_svc()
    git_svc.get_branch_sha.return_value = "abc123"
    git_svc.is_working_tree_clean.return_value = True
    git_svc.list_worktrees_with_branches.side_effect = [
        [(checkout_path, "main")],
        [],
    ]

    display = RecordingStatusDisplay()
    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, status_display=display
    )
    cache = PreflightCache()

    with patch("pycastle.iteration._utils.asyncio.sleep", new_callable=AsyncMock):
        result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightReady)
    printed = [call for call in display.calls if call[0] == "print"]
    assert any(str(checkout_path) in str(call) for call in printed)


# ── get_safe_sha: parallel callers serialise ─────────────────────────────────


def test_get_safe_sha_parallel_callers_run_preflight_once(
    tmp_path, git_svc, github_svc
):
    """Concurrent callers at the same SHA must serialise on the lock and observe
    a single preflight run — only one run_preflight call total."""
    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    async def _run_two():
        return await asyncio.gather(
            cache.get_safe_sha(deps),
            cache.get_safe_sha(deps),
        )

    results = asyncio.run(_run_two())

    assert all(isinstance(r, PreflightReady) for r in results)
    assert results[0] is results[1]
    assert len(fake.preflight_calls) == 1


# ── get_safe_sha: divergence resolution via agent ────────────────────────────


def test_get_safe_sha_resolves_divergence_via_agent_and_returns_ready(
    tmp_path, git_svc, github_svc
):
    """When operating branch has diverged, get_safe_sha spawns the
    divergence-resolution agent; on success it advances the branch ref and
    returns PreflightReady with the post-merge SHA."""
    git_svc.refresh_operating_branch.return_value = OperatingBranchDiverged()
    git_svc.get_branch_sha.return_value = "merged-sha"

    fake = FakeAgentRunner([CompletionOutput()], preflight_responses=[[]])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightReady)
    assert result.sha == "merged-sha"
    git_svc.advance_branch_ref.assert_called_once()


def test_get_safe_sha_divergence_resolver_uses_merge_override_service(
    tmp_path, git_svc, github_svc
):
    """The divergence-resolver RunRequest uses the merge stage override's service."""
    git_svc.refresh_operating_branch.return_value = OperatingBranchDiverged()

    fake = FakeAgentRunner([CompletionOutput()], preflight_responses=[[]])
    deps = _make_deps(
        tmp_path,
        fake,
        git_svc=git_svc,
        github_svc=github_svc,
        cfg=Config(
            max_parallel=4,
            max_iterations=1,
            merge_override=StageOverride(service="codex", effort="medium"),
        ),
    )
    cache = PreflightCache()

    result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightReady)
    assert fake.calls[0].service == "codex"


def test_get_safe_sha_dispatches_divergence_resolver_for_operating_branch(
    tmp_path, git_svc, github_svc
):
    git_svc.refresh_operating_branch.return_value = OperatingBranchDiverged()

    fake = FakeAgentRunner([CompletionOutput()], preflight_responses=[[]])
    deps = _make_deps(
        tmp_path,
        fake,
        git_svc=git_svc,
        github_svc=github_svc,
        cfg=Config(working_branch="pycastle/issue-1484"),
    )
    cache = PreflightCache()

    result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightReady)
    assert fake.calls[0].prompt.template == PromptTemplate.DIVERGENCE_RESOLVE
    assert fake.calls[0].role == AgentRole.DIVERGENCE_RESOLVER
    assert fake.calls[0].work_body == "Resolving divergence"
    git_svc.get_current_branch.assert_not_called()


def test_get_safe_sha_routes_divergence_recovery_through_sandbox_identity(
    tmp_path, git_svc, github_svc
):
    git_svc.refresh_operating_branch.return_value = OperatingBranchDiverged()

    async def _resolve(request):
        RoleSession(request.mount_path, AgentRole.DIVERGENCE_RESOLVER).start_fresh()
        return CompletionOutput()

    fake = FakeAgentRunner(side_effect=_resolve, preflight_responses=[[]])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result = asyncio.run(cache.get_safe_sha(deps))

    expected_identity = worktree_identity("pycastle/diverge-sandbox", tmp_path)

    assert isinstance(result, PreflightReady)
    assert fake.calls[0].mount_path == expected_identity.path
    git_svc.advance_branch_ref.assert_called_once_with(
        tmp_path,
        "main",
        "pycastle/diverge-sandbox",
    )
    assert (
        RoleSession(
            expected_identity.path,
            AgentRole.DIVERGENCE_RESOLVER,
        ).path.exists()
        is False
    )


def test_get_safe_sha_propagates_git_command_error_when_divergence_agent_fails(
    tmp_path, git_svc, github_svc
):
    """When the divergence agent fails (FailedOutput), a GitCommandError
    propagates and no PreflightReady is returned."""
    from pycastle.agents.output_protocol import FailedOutput

    git_svc.refresh_operating_branch.return_value = OperatingBranchDiverged()

    fake = FakeAgentRunner([FailedOutput()], preflight_responses=[])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    with pytest.raises(GitCommandError) as exc_info:
        asyncio.run(cache.get_safe_sha(deps))

    assert "diverged" in str(exc_info.value).lower()


def test_get_safe_sha_propagates_refresh_error_without_spawning_agent(
    tmp_path, git_svc, github_svc
):
    """Refresh errors are propagated immediately without spawning
    the divergence-resolution agent."""
    git_svc.refresh_operating_branch.side_effect = GitCommandError(
        "git fetch origin failed", stderr="authentication failed"
    )

    fake = FakeAgentRunner([], preflight_responses=[])
    deps = _make_deps(
        tmp_path,
        fake,
        git_svc=git_svc,
        github_svc=github_svc,
        status_display=PlainStatusDisplay(),
    )
    cache = PreflightCache()

    with pytest.raises(GitCommandError):
        asyncio.run(cache.get_safe_sha(deps))

    assert len(fake.calls) == 0


def test_get_safe_sha_does_not_reclassify_non_setup_runner_failures_as_setup(
    tmp_path, git_svc, github_svc
):
    """Non-setup runner failures must keep their existing routing instead of being
    coerced into SetupPhaseError at the cache boundary."""
    fake = FakeAgentRunner(
        [],
        preflight_responses=[DockerError("preflight container stream broke")],
    )
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    with pytest.raises(DockerError, match="preflight container stream broke"):
        asyncio.run(cache.get_safe_sha(deps))


# ── Divergence-Resolver fingerprint gate ──────────────────────────────────────


def test_divergence_resolver_discards_session_when_safe_sha_changes(
    tmp_path, github_svc
):
    """When the safe SHA changes, prepare_fingerprint_gate discards the prior
    Divergence-Resolver session so the agent starts without stale state."""
    old_sha = "sha-v1"
    new_sha = "sha-v2"
    branch = "main"

    sandbox_path = reusable_sandbox_worktree_identity(
        SandboxWorktreeIntent.DIVERGENCE, tmp_path
    ).path
    sandbox_path.mkdir(parents=True, exist_ok=True)

    role_session = RoleSession(sandbox_path, AgentRole.DIVERGENCE_RESOLVER)
    role_session.write_fingerprint(_diverge_fingerprint(old_sha, branch))
    role_session.write_continuation("prior-state")

    resumable_during_run: list[bool] = []

    async def _side_effect(request):
        resumable_during_run.append(
            RoleSession(
                request.mount_path, AgentRole.DIVERGENCE_RESOLVER
            ).is_resumable()
        )
        return CompletionOutput()

    git_svc = _conflict_git_svc(new_sha, branch)
    fake = FakeAgentRunner(side_effect=_side_effect, preflight_responses=[[]])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)

    with patch(
        "pycastle.iteration.preflight.reusable_sandbox_worktree",
        return_value=_FixedSandboxWorktree(sandbox_path),
    ):
        asyncio.run(PreflightCache().get_safe_sha(deps))

    assert resumable_during_run == [False]


def test_divergence_resolver_discards_session_when_diverging_branch_changes(
    tmp_path, github_svc
):
    """When the diverging branch changes (SHA unchanged), prepare_fingerprint_gate
    discards the prior Divergence-Resolver session."""
    sha = "abc123"
    old_branch = "pycastle/issue-old"
    new_branch = "main"

    sandbox_path = reusable_sandbox_worktree_identity(
        SandboxWorktreeIntent.DIVERGENCE, tmp_path
    ).path
    sandbox_path.mkdir(parents=True, exist_ok=True)

    role_session = RoleSession(sandbox_path, AgentRole.DIVERGENCE_RESOLVER)
    role_session.write_fingerprint(_diverge_fingerprint(sha, old_branch))
    role_session.write_continuation("prior-state")

    resumable_during_run: list[bool] = []

    async def _side_effect(request):
        resumable_during_run.append(
            RoleSession(
                request.mount_path, AgentRole.DIVERGENCE_RESOLVER
            ).is_resumable()
        )
        return CompletionOutput()

    git_svc = _conflict_git_svc(sha, new_branch)
    fake = FakeAgentRunner(side_effect=_side_effect, preflight_responses=[[]])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)

    with patch(
        "pycastle.iteration.preflight.reusable_sandbox_worktree",
        return_value=_FixedSandboxWorktree(sandbox_path),
    ):
        asyncio.run(PreflightCache().get_safe_sha(deps))

    assert resumable_during_run == [False]


def test_divergence_resolver_preserves_session_when_sha_and_branch_unchanged(
    tmp_path, github_svc
):
    """When safe SHA and diverging branch are both unchanged, the prior session
    (continuation file) is preserved when the agent runs."""
    sha = "abc123"
    branch = "main"

    sandbox_path = reusable_sandbox_worktree_identity(
        SandboxWorktreeIntent.DIVERGENCE, tmp_path
    ).path
    sandbox_path.mkdir(parents=True, exist_ok=True)

    role_session = RoleSession(sandbox_path, AgentRole.DIVERGENCE_RESOLVER)
    role_session.write_fingerprint(_diverge_fingerprint(sha, branch))
    role_session.write_continuation("prior-state")

    resumable_during_run: list[bool] = []

    async def _side_effect(request):
        resumable_during_run.append(
            RoleSession(
                request.mount_path, AgentRole.DIVERGENCE_RESOLVER
            ).is_resumable()
        )
        return CompletionOutput()

    git_svc = _conflict_git_svc(sha, branch)
    fake = FakeAgentRunner(side_effect=_side_effect, preflight_responses=[[]])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)

    with patch(
        "pycastle.iteration.preflight.reusable_sandbox_worktree",
        return_value=_FixedSandboxWorktree(sandbox_path),
    ):
        asyncio.run(PreflightCache().get_safe_sha(deps))

    assert resumable_during_run == [True]


def test_divergence_resolver_writes_fingerprint_inside_context_before_agent_runs(
    tmp_path, github_svc
):
    """write_fingerprint is called inside the worktree context before the agent runs,
    recording the new safe SHA + diverging branch combination."""
    sha = "abc123"
    branch = "main"

    sandbox_path = reusable_sandbox_worktree_identity(
        SandboxWorktreeIntent.DIVERGENCE, tmp_path
    ).path
    sandbox_path.mkdir(parents=True, exist_ok=True)

    fingerprint_during_run: list[str | None] = []

    async def _side_effect(request):
        fingerprint_during_run.append(
            RoleSession(
                request.mount_path, AgentRole.DIVERGENCE_RESOLVER
            ).read_fingerprint()
        )
        return CompletionOutput()

    git_svc = _conflict_git_svc(sha, branch)
    fake = FakeAgentRunner(side_effect=_side_effect, preflight_responses=[[]])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)

    with patch(
        "pycastle.iteration.preflight.reusable_sandbox_worktree",
        return_value=_FixedSandboxWorktree(sandbox_path),
    ):
        asyncio.run(PreflightCache().get_safe_sha(deps))

    assert fingerprint_during_run == [_diverge_fingerprint(sha, branch)]


# ── Narrow except handlers: divergence resolver propagation ──────────────────


def test_divergence_resolver_non_narrowed_exception_propagates_not_pull_exc(
    tmp_path, github_svc
):
    """The except clause in pull_with_resolution that wraps the divergence-resolver
    block is narrowed; an exception type outside the narrowed set (AgentCredentialFailureError)
    propagates directly instead of being replaced by the original GitCommandError."""
    sha = "abc123"
    branch = "main"
    sandbox_path = reusable_sandbox_worktree_identity(
        SandboxWorktreeIntent.DIVERGENCE, tmp_path
    ).path
    sandbox_path.mkdir(parents=True, exist_ok=True)

    git_svc = _conflict_git_svc(sha, branch)

    cred_err = AgentCredentialFailureError(
        "credentials expired",
        service_name="claude",
        classification="operator_actionable_agent_credential_failure",
    )
    fake = FakeAgentRunner([cred_err], preflight_responses=[])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)

    with (
        patch(
            "pycastle.iteration.preflight.reusable_sandbox_worktree",
            return_value=_FixedSandboxWorktree(sandbox_path),
        ),
        pytest.raises(AgentCredentialFailureError),
    ):
        asyncio.run(PreflightCache().get_safe_sha(deps))
