import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pycastle.agent_result import (
    PreflightFailure,
)
from pycastle.services import ClaudeService
from pycastle.config import Config, StageOverride
from pycastle.errors import ClaudeServiceError, ConfigValidationError, UsageLimitError
from pycastle.services import GitCommandError, GitService
from pycastle.services import GithubNotFoundError, GithubService
from pycastle.iteration._deps import FakeAgentRunner, RecordingStatusDisplay
from pycastle.orchestrator import (
    prune_orphan_worktrees,
    run,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _plan_json(issues: list[dict]) -> str:
    return f"<promise>COMPLETE</promise><plan>{json.dumps({'issues': issues})}</plan>"


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

    def _fake_create_worktree(repo, wt, branch, sha=None):
        wt.mkdir(parents=True, exist_ok=True)
        (wt / "pyproject.toml").write_text("[project]\nname='t'\n")

    mock_svc.create_worktree.side_effect = _fake_create_worktree
    return mock_svc


def _make_github_svc():
    mock = MagicMock(spec=GithubService)
    mock.has_open_issues_with_label.return_value = True
    mock.get_open_issues.return_value = [
        {"number": 1, "title": "Default Issue"},
        {"number": 2, "title": "Default Issue 2"},
    ]
    return mock


def _make_github_svc_afk():
    """GithubService mock for AFK path (verdict comes from agent output label)."""
    mock = MagicMock(spec=GithubService)
    mock.get_issue_title.return_value = "Preflight fix title"
    mock.get_open_issues.return_value = [{"number": 1, "title": "Default Issue"}]
    mock.has_open_issues_with_label.return_value = True
    return mock


def _make_github_svc_hitl():
    """GithubService mock for HITL path (verdict comes from agent output label)."""
    mock = MagicMock(spec=GithubService)
    mock.get_issue_title.return_value = "Preflight fix title"
    mock.get_open_issues.return_value = [{"number": 1, "title": "Default Issue"}]
    mock.has_open_issues_with_label.return_value = True
    return mock


_FAKE_TEST_MODELS = (
    "claude-haiku-4-5",
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-opus-4-7",
)


def _make_claude_svc(models: tuple[str, ...] = _FAKE_TEST_MODELS) -> ClaudeService:
    mock = MagicMock(spec=ClaudeService)
    mock.list_models.return_value = models
    return mock


def _write_config(tmp_path: Path, **kwargs) -> None:
    (tmp_path / "pycastle").mkdir(exist_ok=True)
    lines = ["from pycastle import StageOverride", "from pathlib import Path"]
    for k, v in kwargs.items():
        if isinstance(v, StageOverride):
            lines.append(f"{k} = StageOverride(model={v.model!r}, effort={v.effort!r})")
        elif isinstance(v, Path):
            lines.append(f"{k} = Path({str(v)!r})")
        else:
            lines.append(f"{k} = {v!r}")
    (tmp_path / "pycastle" / "config.py").write_text("\n".join(lines) + "\n")


def _run(
    tmp_path,
    run_agent_fn=None,
    *,
    claude_service=None,
    git_service=None,
    github_service=None,
    agent_runner=None,
    status_display=None,
    **config_kwargs,
):
    config_kwargs.setdefault("max_parallel", 4)
    config_kwargs.setdefault("max_iterations", 1)
    _write_config(tmp_path, **config_kwargs)
    asyncio.run(
        run(
            {},
            tmp_path,
            run_agent=run_agent_fn,
            agent_runner=agent_runner,
            claude_service=claude_service
            if claude_service is not None
            else _make_claude_svc(),
            git_service=git_service if git_service is not None else _make_git_svc(),
            github_service=github_service,
            status_display=status_display,
        )
    )


# ── Issue 193: run() works when planner omits branch field ───────────────────


def test_run_does_not_crash_when_planner_omits_branch_field(tmp_path):
    """run() must not KeyError when planner output has no 'branch' key in issues."""
    dispatched: list[str] = []

    async def _fake_run_agent(name, prompt_args=None, **kwargs):
        if name == "Planner":
            return '<promise>COMPLETE</promise><plan>{"issues": [{"number": 193, "title": "Fix branch bug"}]}</plan>'
        if "Implementer" in name:
            dispatched.append((prompt_args or {}).get("BRANCH", ""))
            return "<promise>COMPLETE</promise>"
        return "<promise>COMPLETE</promise>"

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        github_service=_make_github_svc(),
    )

    assert dispatched == ["pycastle/issue-193"]


# ── Issue 188: deterministic branch names ────────────────────────────────────


def test_run_computes_branch_from_issue_number_not_planner_slug(tmp_path):
    """After parse_plan, each issue branch must be pycastle/issue-N, ignoring planner slug."""
    captured_branches: list[str] = []

    async def _fake_run_agent(name, prompt_args=None, **kwargs):
        if name == "Planner":
            return '<promise>COMPLETE</promise><plan>{"issues": [{"number": 42, "title": "Fix thing"}]}</plan>'
        if "Implementer" in name:
            captured_branches.append((prompt_args or {}).get("BRANCH", ""))
            return "<promise>COMPLETE</promise>"
        return "<promise>COMPLETE</promise>"

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        github_service=_make_github_svc(),
    )

    assert captured_branches == ["pycastle/issue-42"], (
        f"Expected branch pycastle/issue-42; got {captured_branches}"
    )


def test_preflight_issue_branch_uses_pycastle_format(tmp_path):
    """A preflight fix issue must use branch pycastle/issue-N, not issue/N."""
    captured_branches: list[str] = []

    async def _fake_run_agent(name, prompt_args=None, branch=None, **kwargs):
        if "Pre-Flight Reporter" in name:
            return '<issue>{"number": 77, "labels": ["ready-for-agent"]}</issue>'
        if "Implementer" in name:
            captured_branches.append((prompt_args or {}).get("BRANCH", ""))
            return "<promise>COMPLETE</promise>"
        return "<promise>COMPLETE</promise>"

    _run(
        tmp_path,
        agent_runner=FakeAgentRunner(
            side_effect=_fake_run_agent,
            preflight_responses=[(("ruff", "ruff check .", "E501"),)],
        ),
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        github_service=_make_github_svc_afk(),
    )

    assert captured_branches == ["pycastle/issue-77"], (
        f"Expected pycastle/issue-77; got {captured_branches}"
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


# ── Cycle 24-C1/C2: error logging on agent failure ───────────────────────────


def test_failed_agent_appends_traceback_to_errors_log(tmp_path):
    logs_dir = tmp_path / "pycastle" / "logs"
    logs_dir.mkdir(parents=True)
    errors_log = logs_dir / "errors.log"

    boom = RuntimeError("something went wrong")

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json([{"number": 1, "title": "Fix thing"}])
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

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json([{"number": 1, "title": "Fix thing"}])
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

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json([{"number": 1, "title": "Fix thing"}])
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


# ── Issue-78: model resolution called at start of run() ─────────────────────


def test_run_resolves_models_before_any_agent(tmp_path):
    """load_config model resolution must complete before the first run_agent call."""
    call_order: list[str] = []

    svc = MagicMock(spec=ClaudeService)

    def _tracking_list_models():
        call_order.append("resolve")
        return _FAKE_TEST_MODELS

    svc.list_models.side_effect = _tracking_list_models

    (tmp_path / "pycastle").mkdir(exist_ok=True)
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="haiku", effort="")\n'
        "max_parallel = 4\nmax_iterations = 1\n"
    )

    async def _fake_run_agent(*args, **kwargs):
        call_order.append("agent")
        return _plan_json([])

    asyncio.run(
        run(
            {},
            tmp_path,
            run_agent=_fake_run_agent,
            claude_service=svc,
            git_service=_make_git_svc(),
            github_service=_make_github_svc(),
        )
    )

    assert call_order[0] == "resolve", f"resolve must be first; got {call_order}"


def test_run_config_validation_error_propagates_no_agents_started(tmp_path):
    """ConfigValidationError from load_config must propagate and prevent all agents."""
    agents_started: list[str] = []

    async def _fake_run_agent(*args, **kwargs):
        agents_started.append(kwargs.get("name", "?"))
        return "<promise>COMPLETE</promise>"

    svc = MagicMock(spec=ClaudeService)
    svc.list_models.side_effect = ClaudeServiceError("unavailable")

    (tmp_path / "pycastle").mkdir(exist_ok=True)
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="haiku", effort="")\n'
        "max_parallel = 4\nmax_iterations = 1\n"
    )

    with pytest.raises(ConfigValidationError):
        asyncio.run(
            run(
                {},
                tmp_path,
                run_agent=_fake_run_agent,
                claude_service=svc,
                git_service=_make_git_svc(),
            )
        )

    assert agents_started == [], f"No agents must start; got {agents_started}"


# ── Issue-78: model/effort passed per stage ───────────────────────────────────


def test_planner_receives_plan_stage_model_and_effort(tmp_path):
    """Planner run_agent call must include model and effort from plan stage override."""
    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append(
            {"name": name, "model": kwargs.get("model"), "effort": kwargs.get("effort")}
        )
        return _plan_json([])

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        plan_override=StageOverride(model="claude-haiku-4-5", effort="low"),
    )

    planner_call = next(c for c in captured if c["name"] == "Planner")
    assert planner_call["model"] == "claude-haiku-4-5"
    assert planner_call["effort"] == "low"


def test_implementer_receives_implement_stage_model_and_effort(tmp_path):
    """Each Implementer run_agent call must include model and effort from implement stage."""
    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append(
            {"name": name, "model": kwargs.get("model"), "effort": kwargs.get("effort")}
        )
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 1, "title": "Fix"}])

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(),
        github_service=_make_github_svc(),
        implement_override=StageOverride(model="claude-sonnet-4-6", effort="high"),
    )

    impl_call = next(c for c in captured if "Implementer" in c["name"])
    assert impl_call["model"] == "claude-sonnet-4-6"
    assert impl_call["effort"] == "high"


def test_reviewer_receives_review_stage_model_and_effort(tmp_path):
    """Each Reviewer run_agent call must include model and effort from review stage."""
    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append(
            {"name": name, "model": kwargs.get("model"), "effort": kwargs.get("effort")}
        )
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 1, "title": "Fix"}])

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(),
        github_service=_make_github_svc(),
        review_override=StageOverride(model="claude-haiku-4-5", effort="medium"),
    )

    rev_call = next(c for c in captured if "Reviewer" in c["name"])
    assert rev_call["model"] == "claude-haiku-4-5"
    assert rev_call["effort"] == "medium"


def test_merger_receives_merge_stage_model_and_effort(tmp_path):
    """Merger run_agent call must include model and effort from merge stage override."""
    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append(
            {"name": name, "model": kwargs.get("model"), "effort": kwargs.get("effort")}
        )
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 1, "title": "Fix"}])

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[False]),
        github_service=_make_github_svc(),
        merge_override=StageOverride(model="claude-opus-4-7", effort="low"),
    )

    merger_call = next(c for c in captured if c["name"] == "Merger")
    assert merger_call["model"] == "claude-opus-4-7"
    assert merger_call["effort"] == "low"


def test_empty_stage_override_passes_empty_strings(tmp_path):
    """Empty model and effort in stage override must pass empty strings to run_agent."""
    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append(
            {"name": name, "model": kwargs.get("model"), "effort": kwargs.get("effort")}
        )
        return _plan_json([])

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
    )

    planner_call = next(c for c in captured if c["name"] == "Planner")
    assert planner_call["model"] == ""
    assert planner_call["effort"] == ""


def test_stage_overrides_are_independent(tmp_path):
    """Different stages must receive their own independent model/effort values."""
    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append(
            {"name": name, "model": kwargs.get("model"), "effort": kwargs.get("effort")}
        )
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 1, "title": "Fix"}])

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
    assert by_name["Planner"]["model"] == "claude-haiku-4-5"
    assert by_name["Planner"]["effort"] == "low"
    assert by_name["Implementer #1"]["model"] == "claude-sonnet-4-6"
    assert by_name["Implementer #1"]["effort"] == "medium"
    assert by_name["Reviewer #1"]["model"] == "claude-haiku-4-5"
    assert by_name["Reviewer #1"]["effort"] == ""
    assert by_name["Merger"]["model"] == "claude-opus-4-7"
    assert by_name["Merger"]["effort"] == "high"


# ── Issue-100: stage parameter and CHECKS prompt arg ─────────────────────────


def test_merger_receives_checks_prompt_arg_from_preflight_checks(tmp_path):
    """Merger must receive CHECKS built from preflight_checks commands joined by ' && '."""
    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append({"name": name, "prompt_args": kwargs.get("prompt_args", {})})
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 1, "title": "Fix"}])

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[False]),
        github_service=_make_github_svc(),
    )

    merger_call = next(c for c in captured if c["name"] == "Merger")
    expected_checks = " && ".join(cmd for _, cmd in Config().preflight_checks)
    assert merger_call["prompt_args"]["CHECKS"] == expected_checks


def test_each_agent_passes_correct_stage_string(tmp_path):
    """Planner, Implementer, Reviewer, and Merger must each pass the correct stage= string."""
    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append({"name": name, "stage": kwargs.get("stage")})
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 1, "title": "Fix"}])

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[False]),
        github_service=_make_github_svc(),
    )

    by_name = {c["name"]: c for c in captured}
    assert by_name["Planner"]["stage"] == "plan-sandbox"
    assert by_name["Implementer #1"]["stage"] == "pre-implementation"
    assert by_name["Reviewer #1"]["stage"] == "pre-review"
    assert by_name["Merger"]["stage"] == "pre-merge"


# ── Issue-95: parallel implementers with bounded concurrency ──────────────────


def test_multiple_implementers_run_in_parallel(tmp_path):
    """With MAX_PARALLEL >= N issues, all N implementers must be active simultaneously."""
    active_implementers: set[str] = set()
    max_concurrent = 0

    async def _fake_run_agent(name, prompt_file, mount_path, prompt_args=None, **kw):
        nonlocal max_concurrent
        if name == "Planner":
            return _plan_json(
                [
                    {"number": i, "title": f"Issue {i}", "branch": f"issue/{i}"}
                    for i in range(1, 4)
                ]
            )
        if "Implementer" in name:
            active_implementers.add(name)
            max_concurrent = max(max_concurrent, len(active_implementers))
            await asyncio.sleep(0.05)
            active_implementers.discard(name)
            return "<promise>COMPLETE</promise>"
        return "<promise>COMPLETE</promise>"

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(),
        github_service=_make_github_svc(),
    )

    assert max_concurrent == 3, (
        f"Expected all 3 implementers active simultaneously, max was {max_concurrent}"
    )


def test_concurrent_agents_never_exceed_max_parallel(tmp_path):
    """The total number of concurrently active agents must never exceed MAX_PARALLEL."""
    active_count = 0
    max_active = 0
    max_parallel = 3

    async def _fake_run_agent(name, prompt_file, mount_path, prompt_args=None, **kw):
        nonlocal active_count, max_active
        if name == "Planner":
            return _plan_json(
                [
                    {"number": i, "title": f"Issue {i}", "branch": f"issue/{i}"}
                    for i in range(1, 8)
                ]
            )
        active_count += 1
        max_active = max(max_active, active_count)
        await asyncio.sleep(0.01)
        active_count -= 1
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return "<promise>COMPLETE</promise>"

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(),
        github_service=_make_github_svc(),
        max_parallel=max_parallel,
    )

    assert max_active <= max_parallel, (
        f"Active agents exceeded MAX_PARALLEL={max_parallel}: max observed={max_active}"
    )


def test_implementer_starts_while_reviewer_runs(tmp_path):
    """A new Implementer must be able to start while a prior issue's Reviewer is running."""
    events: list[str] = []

    async def _fake_run_agent(name, prompt_file, mount_path, prompt_args=None, **kw):
        if name == "Planner":
            return _plan_json(
                [
                    {"number": i, "title": f"Issue {i}", "branch": f"issue/{i}"}
                    for i in range(1, 4)
                ]
            )
        events.append(f"start:{name}")
        await asyncio.sleep(0.03)
        events.append(f"end:{name}")
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return "<promise>COMPLETE</promise>"

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(),
        github_service=_make_github_svc(),
        max_parallel=3,
    )

    impl_3_start = next(
        (i for i, e in enumerate(events) if e == "start:Implementer #3"), None
    )
    rev_1_end = next((i for i, e in enumerate(events) if e == "end:Reviewer #1"), None)

    assert impl_3_start is not None, "Implementer #3 must start"
    assert rev_1_end is not None, "Reviewer #1 must finish"
    assert impl_3_start < rev_1_end, (
        f"Implementer #3 must start before Reviewer #1 finishes; events={events}"
    )


# ── Issue-101: sequential merge loop with post-merge checks ──────────────────


def test_clean_merges_skip_merger(tmp_path):
    """When all branches merge cleanly, Merger agent must NOT be spawned."""
    agent_names: list[str] = []

    issues = [
        {"number": 1, "title": "Fix A"},
        {"number": 2, "title": "Fix B"},
    ]

    async def _fake_run_agent(name, **kwargs):
        agent_names.append(name)
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json(issues)

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True, True]),
        github_service=_make_github_svc(),
    )

    assert "Merger" not in agent_names, (
        f"Merger must not be spawned on clean merges; agents called: {agent_names}"
    )


def test_clean_merge_calls_close_issue_per_issue_and_close_completed_parent_issues(
    tmp_path,
):
    """Each cleanly-merged issue must be closed via close_issue(); close_completed_parent_issues()
    must be called once after all merges."""
    issues = [
        {"number": 7, "title": "Fix A"},
        {"number": 8, "title": "Fix B"},
    ]

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json(issues)

    mock_github = _make_github_svc()
    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True, True]),
        github_service=mock_github,
    )

    closed = [call.args[0] for call in mock_github.close_issue.call_args_list]
    assert sorted(closed) == [7, 8], f"Expected issues 7 and 8 closed; got {closed}"
    assert mock_github.close_completed_parent_issues.call_count == 1, (
        "close_completed_parent_issues must be called once after all merges"
    )


def test_conflict_branch_spawns_merger_with_only_failing_branch(tmp_path):
    """When one branch conflicts, Merger is spawned with only the conflicting branch."""
    captured: list[dict] = []

    issues = [
        {"number": 1, "title": "Clean"},
        {"number": 2, "title": "Conflict"},
    ]

    async def _fake_run_agent(name, **kwargs):
        captured.append({"name": name, "prompt_args": kwargs.get("prompt_args", {})})
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json(issues)

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True, False]),
        github_service=_make_github_svc(),
    )

    merger_calls = [c for c in captured if c["name"] == "Merger"]
    assert len(merger_calls) == 1, (
        f"Expected exactly one Merger call; got {merger_calls}"
    )
    branches_arg = merger_calls[0]["prompt_args"]["BRANCHES"]
    assert "pycastle/issue-2" in branches_arg
    assert "pycastle/issue-1" not in branches_arg


def test_conflict_branch_closed_after_merger_agent(tmp_path):
    """Conflicting branches must be closed by the orchestrator after the Merger agent returns."""
    issues = [
        {"number": 1, "title": "Clean"},
        {"number": 2, "title": "Conflict"},
    ]

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json(issues)

    mock_github = _make_github_svc()
    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True, False]),
        github_service=mock_github,
    )

    closed = [call.args[0] for call in mock_github.close_issue.call_args_list]
    assert 2 in closed, (
        f"Conflict issue #2 must be closed after Merger; closed: {closed}"
    )
    assert 1 in closed, f"Clean issue #1 must also be closed; closed: {closed}"


def test_conflict_merge_calls_close_completed_parent_issues(tmp_path):
    """After conflict branches are merged, close_completed_parent_issues must be called once."""
    issues = [{"number": 5, "title": "Conflict"}]

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json(issues)

    mock_github = _make_github_svc()
    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[False]),
        github_service=mock_github,
    )

    assert mock_github.close_completed_parent_issues.call_count == 1, (
        "close_completed_parent_issues must be called exactly once after conflict merge"
    )


def test_merger_does_not_receive_issues_prompt_arg(tmp_path):
    """Merger must not receive an ISSUES prompt arg — issue closing is the orchestrator's job."""
    captured: list[dict] = []

    issues = [
        {"number": 3, "title": "Clean issue"},
        {"number": 4, "title": "Conflict issue"},
    ]

    async def _fake_run_agent(name, **kwargs):
        captured.append({"name": name, "prompt_args": kwargs.get("prompt_args", {})})
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json(issues)

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True, False]),
        github_service=_make_github_svc(),
    )

    merger_calls = [c for c in captured if c["name"] == "Merger"]
    assert len(merger_calls) == 1
    assert "ISSUES" not in merger_calls[0]["prompt_args"], (
        "Merger must not receive an ISSUES prompt arg"
    )


def test_multiple_conflict_issues_all_closed_after_merger(tmp_path):
    """Each conflict issue must be individually closed when there are multiple conflicts."""
    issues = [
        {"number": 10, "title": "Conflict A"},
        {"number": 11, "title": "Conflict B"},
        {"number": 12, "title": "Conflict C"},
    ]

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json(issues)

    mock_github = _make_github_svc()
    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[False, False, False]),
        github_service=mock_github,
    )

    closed = [call.args[0] for call in mock_github.close_issue.call_args_list]
    assert 10 in closed, f"Conflict issue #10 must be closed; closed: {closed}"
    assert 11 in closed, f"Conflict issue #11 must be closed; closed: {closed}"
    assert 12 in closed, f"Conflict issue #12 must be closed; closed: {closed}"
    assert mock_github.close_completed_parent_issues.call_count == 1


def test_preflight_issue_receives_correct_command_and_output(tmp_path):
    """preflight-issue agent must receive exact COMMAND and OUTPUT from the failing check."""
    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append({"name": name, "prompt_args": kwargs.get("prompt_args", {})})
        if "Pre-Flight Reporter" in name:
            return '<issue>{"number": 70, "labels": ["ready-for-human"]}</issue>'
        return "<promise>COMPLETE</promise>"

    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            agent_runner=FakeAgentRunner(
                side_effect=_fake_run_agent,
                preflight_responses=[
                    (
                        (
                            "pytest",
                            "pytest -x",
                            "FAILED tests/test_bar.py::test_something",
                        ),
                    )
                ],
            ),
            github_service=_make_github_svc_hitl(),
        )

    pf_calls = [c for c in captured if "Pre-Flight Reporter" in c["name"]]
    assert len(pf_calls) == 1
    args = pf_calls[0]["prompt_args"]
    assert args.get("COMMAND") == "pytest -x", (
        f"COMMAND must be 'pytest -x'; got {args.get('COMMAND')!r}"
    )
    assert args.get("OUTPUT") == "FAILED tests/test_bar.py::test_something", (
        f"OUTPUT mismatch; got {args.get('OUTPUT')!r}"
    )


def test_clean_merged_branches_are_deleted_after_try_merge(tmp_path):
    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 1, "title": "Fix A"}])

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

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 2, "title": "Conflict"}])

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

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 1, "title": "Fix A"}])

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

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 1, "title": "Fix A"}])

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

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json([{"number": 1, "title": "Fix"}])
        return ""  # implementer does not return COMPLETE

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

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json([{"number": 1, "title": "Fix"}])
        raise RuntimeError("agent failed")

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        logs_dir=logs_dir,
    )

    assert (logs_dir / "errors.log").exists()


# ── Issue-175: safe SHA pinning and skip-preflight logic ──────────────────────


def test_safe_sha_pinned_and_passed_to_implementer_after_preplanning_preflight(
    tmp_path,
):
    """After plan-sandbox preflight passes, the HEAD SHA must be captured and passed to implementers."""
    captured_shas: list[str | None] = []
    fake_sha = "deadbeef123"

    mock_git = _make_git_svc(try_merge_side_effect=[True])
    mock_git.get_head_sha.return_value = fake_sha

    async def _fake_run_agent(name, sha=None, **kwargs):
        if "Implementer" in name:
            captured_shas.append(sha)
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 1, "title": "Fix"}])

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=mock_git,
        github_service=_make_github_svc(),
    )

    assert captured_shas == [fake_sha], (
        f"Implementer must receive sha={fake_sha!r}; got {captured_shas}"
    )


def test_preplanning_preflight_runs_on_cold_startup(tmp_path):
    """On cold startup the Planner must not be called when get_open_issues returns empty."""
    planner_calls: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            planner_calls.append(name)
            return _plan_json([])
        return "<promise>COMPLETE</promise>"

    github_svc = _make_github_svc()
    github_svc.get_open_issues.return_value = []
    _run(tmp_path, _fake_run_agent, github_service=github_svc)

    assert len(planner_calls) == 0, (
        f"Expected 0 Planner calls; got {len(planner_calls)}"
    )


def test_pinned_sha_is_passed_to_each_implementer(tmp_path):
    """The pinned SHA must be passed as sha= to run_agent for every implementer in the batch."""
    captured_calls: list[dict] = []
    fake_sha = "cafebabe000"

    mock_git = _make_git_svc(try_merge_side_effect=[True, True])
    mock_git.get_head_sha.return_value = fake_sha

    issues = [
        {"number": 1, "title": "Fix A"},
        {"number": 2, "title": "Fix B"},
    ]

    async def _fake_run_agent(name, sha=None, **kwargs):
        captured_calls.append({"name": name, "sha": sha})
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json(issues)

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=mock_git,
        github_service=_make_github_svc(),
    )

    impl_calls = [c for c in captured_calls if "Implementer" in c["name"]]
    assert len(impl_calls) == 2, f"Expected 2 implementer calls; got {impl_calls}"
    for call in impl_calls:
        assert call["sha"] == fake_sha, (
            f"Implementer {call['name']} must receive sha={fake_sha!r}; got {call['sha']!r}"
        )


# ── Issue-176: preflight failure handling and HITL routing ────────────────────


def test_preflight_failure_afk_planner_skipped_one_implementer(tmp_path):
    """On plan-sandbox preflight failure with AFK verdict, Planner must NOT be called
    and exactly one Implementer must be spawned for the preflight issue."""
    agent_names: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        agent_names.append(name)
        if "Pre-Flight Reporter" in name:
            return '<issue>{"number": 42, "labels": ["ready-for-agent"]}</issue>'
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return "<promise>COMPLETE</promise>"

    _run(
        tmp_path,
        agent_runner=FakeAgentRunner(
            side_effect=_fake_run_agent,
            preflight_responses=[(("ruff", "ruff check .", "E501 line too long"),)],
        ),
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        github_service=_make_github_svc_afk(),
    )

    implementer_calls = [n for n in agent_names if "Implementer" in n]
    assert "Planner" not in agent_names, (
        "Planner must not be called on AFK preflight path"
    )
    assert len(implementer_calls) == 1, (
        f"Exactly one Implementer must be spawned for the preflight fix; got {implementer_calls}"
    )


def test_preflight_failure_hitl_exits_nonzero_no_implementer(tmp_path):
    """On plan-sandbox preflight failure with HITL verdict, process must exit non-zero
    and no Implementer must be spawned."""
    implementer_calls: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        if "Pre-Flight Reporter" in name:
            return '<issue>{"number": 99, "labels": ["ready-for-human"]}</issue>'
        if "Implementer" in name:
            implementer_calls.append(name)
        return "<promise>COMPLETE</promise>"

    with pytest.raises(SystemExit) as exc_info:
        _run(
            tmp_path,
            agent_runner=FakeAgentRunner(
                side_effect=_fake_run_agent,
                preflight_responses=[(("ruff", "ruff check .", "E501"),)],
            ),
            github_service=_make_github_svc_hitl(),
        )

    assert exc_info.value.code != 0, "Exit code must be non-zero for HITL"
    assert implementer_calls == [], (
        f"No Implementer must be spawned on HITL; got {implementer_calls}"
    )


def test_preflight_failure_only_first_check_acted_on(tmp_path):
    """When multiple preflight checks fail, only the first (by order) must be acted on."""
    preflight_issue_calls: list[dict] = []

    async def _fake_run_agent(name, prompt_args=None, **kwargs):
        if "Pre-Flight Reporter" in name:
            preflight_issue_calls.append(
                {"name": name, "prompt_args": prompt_args or {}}
            )
            return '<issue>{"number": 10, "labels": ["ready-for-human"]}</issue>'
        return "<promise>COMPLETE</promise>"

    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            agent_runner=FakeAgentRunner(
                side_effect=_fake_run_agent,
                preflight_responses=[
                    (
                        ("ruff", "ruff check .", "ruff error"),
                        ("mypy", "mypy .", "mypy error"),
                        ("pytest", "pytest", "pytest error"),
                    )
                ],
            ),
            github_service=_make_github_svc_hitl(),
        )

    assert len(preflight_issue_calls) == 1, (
        f"Only one preflight-issue agent must be spawned; got {len(preflight_issue_calls)}"
    )
    args = preflight_issue_calls[0]["prompt_args"]
    assert args.get("CHECK_NAME") == "ruff", (
        f"Must act on first check (ruff); got CHECK_NAME={args.get('CHECK_NAME')!r}"
    )
    assert args.get("COMMAND") == "ruff check .", (
        f"Must use first check's command; got {args.get('COMMAND')!r}"
    )


# ── Issue-183: orchestrator exit handling for usage-limit shutdown ─────────────


def test_usage_limit_error_exits_with_code_1(tmp_path):
    """When UsageLimitError is raised by an agent task, orchestrator must exit with code 1."""

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json([{"number": 1, "title": "Fix"}])
        raise UsageLimitError("")

    with pytest.raises(SystemExit) as exc_info:
        _run(tmp_path, _fake_run_agent, github_service=_make_github_svc())

    assert exc_info.value.code == 1


def test_usage_limit_error_prints_resume_message_to_stderr(tmp_path, capsys):
    """When UsageLimitError is raised by an agent task, the resume message must be printed to stderr."""

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json([{"number": 1, "title": "Fix"}])
        raise UsageLimitError("")

    with pytest.raises(SystemExit):
        _run(tmp_path, _fake_run_agent, github_service=_make_github_svc())

    err = capsys.readouterr().err
    assert (
        "Usage limit reached. Worktrees preserved. Run 'pycastle run' again to resume."
        in err
    )


def test_usage_limit_error_not_written_to_errors_log(tmp_path):
    """UsageLimitError must not be logged to errors.log (unlike regular exceptions)."""

    logs_dir = tmp_path / "pycastle" / "logs"
    logs_dir.mkdir(parents=True)
    errors_log = logs_dir / "errors.log"

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json([{"number": 1, "title": "Fix"}])
        raise UsageLimitError("")

    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            _fake_run_agent,
            github_service=_make_github_svc(),
            logs_dir=logs_dir,
        )

    assert not errors_log.exists() or errors_log.read_text() == "", (
        "UsageLimitError must not be written to errors.log"
    )


def test_usage_limit_error_alongside_regular_exception_exits_with_code_1(
    tmp_path, capsys
):
    """When one task raises UsageLimitError and another raises a regular exception, exit cleanly with code 1."""

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json(
                [{"number": 1, "title": "Limit"}, {"number": 2, "title": "Other"}]
            )
        if "Implementer #1" in name:
            raise UsageLimitError("")
        if "Implementer #2" in name:
            raise RuntimeError("unrelated failure")

    with pytest.raises(SystemExit) as exc_info:
        _run(
            tmp_path,
            _fake_run_agent,
            github_service=_make_github_svc(),
        )

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert (
        "Usage limit reached. Worktrees preserved. Run 'pycastle run' again to resume."
        in err
    )


# ── Issue-194: skip Planner when no ready-for-agent issues exist ──────────────


def test_planner_not_invoked_when_no_ready_for_agent_issues(tmp_path):
    """Planner must not be spawned when has_open_issues_with_label returns False."""
    agent_names: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        agent_names.append(name)
        return "<promise>COMPLETE</promise>"

    mock_github = _make_github_svc()
    mock_github.has_open_issues_with_label.return_value = False

    _run(tmp_path, _fake_run_agent, github_service=mock_github)

    assert "Planner" not in agent_names, (
        f"Planner must not be invoked when no ready-for-agent issues exist; agents={agent_names}"
    )


def test_skip_message_emitted_before_any_agent_when_no_issues(tmp_path, capsys):
    """'No issues with label ... found. Skipping.' must be printed and no agent must run."""
    agent_names: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        agent_names.append(name)
        return "<promise>COMPLETE</promise>"

    mock_github = _make_github_svc()
    mock_github.has_open_issues_with_label.return_value = False

    _run(tmp_path, _fake_run_agent, github_service=mock_github)

    out = capsys.readouterr().out
    assert "No issues with label" in out and "found. Skipping." in out, (
        f"Skip message not printed; stdout={out!r}"
    )
    assert agent_names == [], (
        f"No agents must run when there are no matching issues; got {agent_names}"
    )


def test_planner_invoked_when_ready_for_agent_issues_exist(tmp_path):
    """Planner must be spawned when has_open_issues_with_label returns True."""
    agent_names: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        agent_names.append(name)
        if name == "Planner":
            return _plan_json([{"number": 1, "title": "Do thing"}])
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return "<promise>COMPLETE</promise>"

    mock_github = _make_github_svc()
    mock_github.has_open_issues_with_label.return_value = True

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        github_service=mock_github,
    )

    assert "Planner" in agent_names, (
        f"Planner must be invoked when ready-for-agent issues exist; agents={agent_names}"
    )


# ── Issue-200: planner receives OPEN_ISSUES_JSON (not ISSUE_LABEL) ────────────


def test_planner_receives_open_issues_json_not_issue_label(tmp_path):
    """run() must pass OPEN_ISSUES_JSON (not ISSUE_LABEL) in planner prompt_args."""
    captured_planner_args: dict = {}

    async def _fake_run_agent(name, prompt_args=None, **kwargs):
        if name == "Planner":
            captured_planner_args.update(prompt_args or {})
            return _plan_json([{"number": 1, "title": "Fix"}])
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return "<promise>COMPLETE</promise>"

    mock_github = _make_github_svc()
    mock_github.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Fix thing",
            "body": "Blocked by #99\nDo the work.",
            "labels": [],
            "comments": [],
        },
        {"number": 2, "title": "Another issue", "body": ""},
    ]

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        github_service=mock_github,
    )

    assert "OPEN_ISSUES_JSON" in captured_planner_args, (
        "Planner must receive OPEN_ISSUES_JSON in prompt_args"
    )
    assert "ISSUE_LABEL" not in captured_planner_args, (
        "Planner must not receive ISSUE_LABEL in prompt_args"
    )
    assert "Blocked by #99" not in captured_planner_args["OPEN_ISSUES_JSON"], (
        "Stale blocker reference must be stripped from OPEN_ISSUES_JSON"
    )


# ── Issue-204: cfg injection ──────────────────────────────────────────────────


def test_run_stops_after_max_iterations_from_cfg(tmp_path):
    """run() with cfg=Config(max_iterations=2) must stop after 2 iteration cycles."""
    planner_calls = [0]

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            planner_calls[0] += 1
            if planner_calls[0] < 2:
                return _plan_json([{"number": 1, "title": "Fix"}])
            return _plan_json([])
        return "<promise>COMPLETE</promise>"

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

    async def _fake_run_agent(name, **kwargs):
        nonlocal active_count, max_active
        if name == "Planner":
            return _plan_json(
                [{"number": i, "title": f"Issue {i}"} for i in range(1, 6)]
            )
        active_count += 1
        max_active = max(max_active, active_count)
        await asyncio.sleep(0.01)
        active_count -= 1
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return "<promise>COMPLETE</promise>"

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(),
        github_service=_make_github_svc(),
        max_parallel=2,
    )

    assert max_active <= 2, f"Expected at most 2 concurrent; max was {max_active}"


# ── Issue-331: gh CLI not found detected at startup ──────────────────────────


def test_run_exits_with_code_1_when_gh_not_found(tmp_path):
    """run() without an injected github_service must exit 1 if gh is absent."""
    with patch("pycastle.orchestrator.shutil.which", return_value=None):
        with pytest.raises(SystemExit) as exc_info:
            _run(tmp_path)
    assert exc_info.value.code == 1


def test_run_prints_gh_install_and_auth_when_gh_not_found(tmp_path, capsys):
    """run() must print both install command and auth step to stderr when gh is absent."""
    with patch("pycastle.orchestrator.shutil.which", return_value=None):
        with pytest.raises(SystemExit):
            _run(tmp_path)
    err = capsys.readouterr().err
    assert "sudo apt install gh" in err
    assert "gh auth login" in err


def test_run_no_agents_start_when_gh_not_found(tmp_path):
    """run() must not spawn any agents when gh is absent."""
    agents_started: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        agents_started.append(name)
        return "<promise>COMPLETE</promise>"

    with patch("pycastle.orchestrator.shutil.which", return_value=None):
        with pytest.raises(SystemExit):
            _run(tmp_path, _fake_run_agent)

    assert agents_started == [], f"No agents must start; got {agents_started}"


def test_run_raises_github_not_found_error_when_gh_invocation_fails(tmp_path):
    """run() must propagate GithubNotFoundError when gh is in PATH but invoking it raises FileNotFoundError."""
    with (
        patch("pycastle.orchestrator.shutil.which", return_value="/usr/bin/gh"),
        patch("subprocess.run", side_effect=FileNotFoundError),
    ):
        with pytest.raises(GithubNotFoundError):
            _run(tmp_path)


def test_run_skips_gh_check_when_github_service_injected(tmp_path):
    """run() must not check for gh CLI when a github_service is already injected."""

    async def _fake_run_agent(name, **kwargs):
        return _plan_json([])

    with patch("pycastle.orchestrator.shutil.which", return_value=None):
        _run(
            tmp_path, _fake_run_agent, github_service=_make_github_svc()
        )  # must not raise SystemExit


def test_run_with_empty_repo_root_completes(tmp_path):
    """run() with empty repo_root completes without error using default config."""

    async def _fake_run_agent(name, **kwargs):
        return _plan_json([])

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

    async def _fake_run_agent(name, **kwargs):
        agents_started.append(name)
        return "<promise>COMPLETE</promise>"

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

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            captured_planner["model"] = kwargs.get("model")
            captured_planner["effort"] = kwargs.get("effort")
            return _plan_json([])
        return "<promise>COMPLETE</promise>"

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        plan_override=StageOverride(model="claude-haiku-4-5", effort="low"),
    )

    assert captured_planner.get("model") == "claude-haiku-4-5"
    assert captured_planner.get("effort") == "low"


def test_run_model_shorthand_resolved_before_agent_calls(tmp_path):
    """Model shorthand must be resolved to a full model ID before it reaches agent calls."""
    captured_model: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            captured_model.append(kwargs.get("model", ""))
            return _plan_json([])
        return "<promise>COMPLETE</promise>"

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        plan_override=StageOverride(model="haiku", effort=""),
    )

    assert captured_model == ["claude-haiku-4-5-20251001"], (
        "model shorthand resolution must be propagated to the agent call"
    )


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

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            call_order.append("Planner")
            return _plan_json([{"number": 1, "title": "Fix"}])
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return "<promise>COMPLETE</promise>"

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
    """get_head_sha must be called before each Planner call across multiple iterations."""
    call_order: list[str] = []
    planner_count = [0]

    mock_git = _make_git_svc(try_merge_side_effect=[True])
    mock_git.get_head_sha.side_effect = lambda _: (
        call_order.append("get_head_sha") or "sha"
    )

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            planner_count[0] += 1
            call_order.append(f"Planner-{planner_count[0]}")
            if planner_count[0] == 1:
                return _plan_json([{"number": 1, "title": "Fix"}])
            return _plan_json([])
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return "<promise>COMPLETE</promise>"

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=mock_git,
        github_service=_make_github_svc(),
        max_iterations=2,
    )

    sha_indices = [i for i, e in enumerate(call_order) if e == "get_head_sha"]
    planner_indices = [i for i, e in enumerate(call_order) if e.startswith("Planner")]
    assert len(sha_indices) == 2, (
        f"get_head_sha must be called once per iteration; order={call_order}"
    )
    assert len(planner_indices) == 2, (
        f"Planner must be called twice; order={call_order}"
    )
    for sha_idx, planner_idx in zip(sha_indices, planner_indices):
        assert sha_idx < planner_idx, (
            f"get_head_sha must precede Planner each iteration; order={call_order}"
        )


# ── Issue-187: implementer and reviewer skip preflight ───────────────────────


def test_implementer_preflight_error_siblings_complete(tmp_path):
    """An implementer PreflightFailure must not prevent sibling issues from completing."""
    completed_issues: list[int] = []

    issues = [
        {"number": 1, "title": "Issue one"},
        {"number": 2, "title": "Issue two"},
    ]

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json(issues)
        if name == "Implementer #1":
            return PreflightFailure(failures=(("ruff", "ruff check .", "E501"),))
        if "Implementer" in name:
            completed_issues.append(int(name.split("#")[1]))
            return "<promise>COMPLETE</promise>"
        return "<promise>COMPLETE</promise>"

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(),
        github_service=_make_github_svc(),
    )

    assert 2 in completed_issues, (
        f"Issue #2 must complete; completed: {completed_issues}"
    )


def test_implementer_preflight_error_logs_check_details(tmp_path, capsys):
    """An implementer PreflightFailure must print the failed check name and command to stdout."""

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json([{"number": 3, "title": "Fix types"}])
        return PreflightFailure(
            failures=(("mypy", "mypy .", "error: Cannot find module"),)
        )

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        logs_dir=logs_dir,
    )

    out = capsys.readouterr().out
    assert "mypy" in out
    assert "mypy ." in out
    assert "[('mypy'" not in out, (
        "Output must not be raw tuple repr — format each check explicitly"
    )


# ── Issue-206: worktree SHA + has_open_issues_with_label ──────────────────────


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
    mock_github.close_issue.side_effect = lambda n: closed_issues.append(n)

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json([{"number": 1, "title": "Fix thing"}])
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return "<promise>COMPLETE</promise>"

    (git_repo / "pycastle").mkdir(exist_ok=True)
    (git_repo / "pycastle" / "config.py").write_text(
        "max_parallel = 4\nmax_iterations = 1\n"
    )
    asyncio.run(
        run(
            {},
            git_repo,
            run_agent=_fake_run_agent,
            claude_service=_make_claude_svc(),
            github_service=mock_github,
        )
    )

    assert 1 in closed_issues, (
        f"Issue #1 must be closed after merge; closed={closed_issues}"
    )


# ── Issue-52: Planner preflight error HITL/AFK routing ───────────────────────


def test_planner_preflight_error_spawns_no_implementers(tmp_path):
    """On plan-sandbox PreflightError with HITL verdict, run must exit immediately."""

    async def _fake_run_agent(name, **kwargs):
        return '<issue>{"number": 77, "labels": ["ready-for-human"]}</issue>'

    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            agent_runner=FakeAgentRunner(
                side_effect=_fake_run_agent,
                preflight_responses=[(("ruff", "ruff check .", "E501 line too long"),)],
            ),
            github_service=_make_github_svc_hitl(),
        )


def test_planner_preflight_error_message_names_issue_number(tmp_path, capsys):
    """HITL preflight failure must print a message referencing the filed issue number."""

    async def _fake_run_agent(name, **kwargs):
        return '<issue>{"number": 88, "labels": ["ready-for-human"]}</issue>'

    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            agent_runner=FakeAgentRunner(
                side_effect=_fake_run_agent,
                preflight_responses=[(("ruff", "ruff check .", "E501 line too long"),)],
            ),
            github_service=_make_github_svc_hitl(),
        )

    out = capsys.readouterr().out
    assert "88" in out, f"Output must reference the filed issue number; got: {out!r}"


# ── StatusDisplay routing ─────────────────────────────────────────────────────




# ── Issue-352: startup row ────────────────────────────────────────────────────


def test_startup_row_added_before_checks_and_removed_after_success(tmp_path):
    """run() must add a 'startup' row before any startup check and remove it after all checks pass."""
    recording = RecordingStatusDisplay()

    async def _fake_run_agent(name, **kwargs):
        return _plan_json([])

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        status_display=recording,
    )

    add_idx = next(
        (i for i, c in enumerate(recording.calls) if c[:2] == ("add_agent", "startup")),
        None,
    )
    remove_idx = next(
        (i for i, c in enumerate(recording.calls) if c == ("remove_agent", "startup")),
        None,
    )
    assert add_idx is not None, "startup row must be added"
    assert remove_idx is not None, "startup row must be removed"
    assert add_idx < remove_idx, "startup must be added before it is removed"


def test_startup_row_phase_cycles_git_identity_then_credentials(tmp_path):
    """startup row must start with 'Git identity' phase then update to 'Credentials'."""
    recording = RecordingStatusDisplay()

    async def _fake_run_agent(name, **kwargs):
        return _plan_json([])

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        status_display=recording,
    )

    add_idx = next(
        (
            i
            for i, c in enumerate(recording.calls)
            if c[0] == "add_agent" and c[1] == "startup"
        ),
        None,
    )
    update_idx = next(
        (
            i
            for i, c in enumerate(recording.calls)
            if c[0] == "update_phase" and c[1] == "startup"
        ),
        None,
    )
    assert add_idx is not None and recording.calls[add_idx][2] == "Git identity"
    assert update_idx is not None and recording.calls[update_idx][2] == "Credentials"
    assert add_idx < update_idx, (
        "phase must be updated to 'Credentials' after the row is added with 'Git identity'"
    )


def test_startup_row_removed_when_git_identity_check_fails(tmp_path):
    """startup row must be removed before exit when git identity check fails."""
    recording = RecordingStatusDisplay()

    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            git_service=_make_git_svc_no_user_name(),
            github_service=_make_github_svc(),
            status_display=recording,
        )

    assert ("remove_agent", "startup") in recording.calls


def test_startup_row_removed_when_credentials_check_fails(tmp_path):
    """startup row must be removed before exit when gh CLI is not found."""
    recording = RecordingStatusDisplay()

    with patch("pycastle.orchestrator.shutil.which", return_value=None):
        with pytest.raises(SystemExit):
            _run(tmp_path, status_display=recording)

    assert ("remove_agent", "startup") in recording.calls
