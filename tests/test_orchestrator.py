import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pycastle.agent_result import (
    AgentIncomplete,
    AgentSuccess,
    PreflightFailure,
    UsageLimitHit,
)
from pycastle.config import Config, StageOverride
from pycastle.errors import ConfigValidationError, PreflightError
from pycastle.git_service import GitCommandError, GitService
from pycastle.github_service import GithubService
from pycastle.orchestrator import (
    MERGE_SANDBOX,
    Deps,
    ImplementResult,
    IterationState,
    MergeResult,
    PlanResult,
    _stage_for_agent,
    branch_for,
    delete_merged_branches,
    implement_phase,
    merge_phase,
    parse_plan,
    plan_phase,
    preflight_phase,
    prune_orphan_worktrees,
    run,
    run_issue,
    strip_stale_blocker_refs,
    wait_for_clean_working_tree,
)


# ── IterationState ───────────────────────────────────────────────────────────


def test_iteration_state_defaults_to_none():
    state = IterationState()
    assert state.worktree_sha is None


def test_iteration_state_is_frozen():
    from dataclasses import FrozenInstanceError

    with pytest.raises(FrozenInstanceError):
        state = IterationState(worktree_sha="abc123")
        state.worktree_sha = "other"  # type: ignore[misc]


# ── PlanResult / ImplementResult / MergeResult ───────────────────────────────


def test_plan_result_stores_issues():
    issues = [{"number": 1, "title": "Fix bug"}]
    result = PlanResult(issues=issues)
    assert result.issues == issues


def test_implement_result_stores_completed_and_errors():
    exc = ValueError("oops")
    result = ImplementResult(completed=[{"number": 1}], errors=[({"number": 2}, exc)])
    assert result.completed == [{"number": 1}]
    assert result.errors[0][1] is exc


def test_merge_result_stores_clean_and_conflicts():
    result = MergeResult(clean=[{"number": 1}], conflicts=[{"number": 2}])
    assert result.clean == [{"number": 1}]
    assert result.conflicts == [{"number": 2}]


# ── parse_plan ────────────────────────────────────────────────────────────────


def test_parse_plan_returns_issues_list():
    output = '<plan>{"issues": [{"number": 1, "title": "Fix bug"}]}</plan>'
    assert parse_plan(output) == [{"number": 1, "title": "Fix bug"}]


def test_parse_plan_returns_empty_list_when_no_issues():
    output = '<plan>{"issues": []}</plan>'
    assert parse_plan(output) == []


def test_parse_plan_failure_is_typed_when_no_plan_tag():
    from pycastle.agent_result import PlanParseFailure

    result = parse_plan("some output with no plan tag")
    assert isinstance(result, PlanParseFailure)
    assert "no <plan> tag" in result.detail


def test_parse_plan_returns_unblocked_issues_list():
    output = '<plan>{"unblocked_issues": [{"number": 2, "title": "Do thing"}], "blocked_issues": [{"number": 3, "title": "Later"}]}</plan>'
    assert parse_plan(output) == [{"number": 2, "title": "Do thing"}]


def test_parse_plan_failure_is_typed_when_issues_key_missing():
    from pycastle.agent_result import PlanParseFailure

    output = '<plan>{"something_else": []}</plan>'
    result = parse_plan(output)
    assert isinstance(result, PlanParseFailure)
    assert "unblocked_issues" in result.detail or "issues" in result.detail


def test_parse_plan_failure_is_typed_when_json_is_malformed():
    from pycastle.agent_result import PlanParseFailure

    output = "<plan>this is not valid json</plan>"
    result = parse_plan(output)
    assert isinstance(result, PlanParseFailure)
    assert "malformed JSON" in result.detail


def test_parse_plan_issues_have_no_branch_key():
    output = '<plan>{"issues": [{"number": 5, "title": "Add feature", "branch": "stale/branch"}]}</plan>'
    issues = parse_plan(output)
    assert all("branch" not in issue for issue in issues)


def test_parse_plan_unblocked_issues_have_no_branch_key():
    output = '<plan>{"unblocked_issues": [{"number": 6, "title": "Add feature", "branch": "stale/branch"}]}</plan>'
    issues = parse_plan(output)
    assert all("branch" not in issue for issue in issues)


# ── strip_stale_blocker_refs ──────────────────────────────────────────────────


def test_strip_stale_blocker_refs_removes_line_referencing_closed_blocker():
    issues = [
        {
            "number": 10,
            "title": "Fix bug",
            "body": "Do stuff.\nBlocked by #99\nMore stuff.",
        }
    ]
    result = strip_stale_blocker_refs(issues)
    assert result[0]["body"] == "Do stuff.\nMore stuff."


def test_strip_stale_blocker_refs_handles_none_body():
    issues = [{"number": 10, "title": "Fix bug", "body": None}]
    result = strip_stale_blocker_refs(issues)
    assert result[0]["body"] == ""


def test_strip_stale_blocker_refs_preserves_line_referencing_open_blocker():
    issues = [
        {"number": 10, "title": "Fix bug", "body": "Blocked by #20"},
        {"number": 20, "title": "Dep", "body": ""},
    ]
    result = strip_stale_blocker_refs(issues)
    assert result[0]["body"] == "Blocked by #20"


def test_strip_stale_blocker_refs_multi_number_line_kept_when_any_open():
    issues = [
        {"number": 10, "title": "Fix bug", "body": "Blocked by #20 and #30"},
        {"number": 20, "title": "Dep", "body": ""},
    ]
    result = strip_stale_blocker_refs(issues)
    assert result[0]["body"] == "Blocked by #20 and #30"


def test_strip_stale_blocker_refs_multi_number_line_stripped_when_all_closed():
    issues = [
        {"number": 10, "title": "Fix bug", "body": "Blocked by #88 and #99"},
    ]
    result = strip_stale_blocker_refs(issues)
    assert result[0]["body"] == ""


def test_strip_stale_blocker_refs_case_insensitive():
    issues = [{"number": 10, "title": "Fix bug", "body": "blocked by #99"}]
    result = strip_stale_blocker_refs(issues)
    assert result[0]["body"] == ""


def test_strip_stale_blocker_refs_case_insensitive_all_caps():
    issues = [{"number": 10, "title": "Fix bug", "body": "BLOCKED BY #99"}]
    result = strip_stale_blocker_refs(issues)
    assert result[0]["body"] == ""


def test_strip_stale_blocker_refs_empty_list():
    assert strip_stale_blocker_refs([]) == []


def test_strip_stale_blocker_refs_does_not_mutate_input():
    original_body = "Blocked by #99\nKeep this."
    issues = [{"number": 10, "title": "Fix bug", "body": original_body}]
    strip_stale_blocker_refs(issues)
    assert issues[0]["body"] == original_body


# ── branch_for ───────────────────────────────────────────────────────────────


def test_branch_for_returns_pycastle_issue_format():
    assert branch_for(193) == "pycastle/issue-193"


def test_branch_for_uses_issue_number():
    assert branch_for(1) == "pycastle/issue-1"
    assert branch_for(42) == "pycastle/issue-42"


# ── Issue 193: run() works when planner omits branch field ───────────────────


def test_run_does_not_crash_when_planner_omits_branch_field(tmp_path):
    """run() must not KeyError when planner output has no 'branch' key in issues."""
    dispatched: list[str] = []

    async def _fake_run_agent(name, prompt_args=None, **kwargs):
        if name == "Planner":
            return AgentIncomplete(
                partial_output='<plan>{"issues": [{"number": 193, "title": "Fix branch bug"}]}</plan>'
            )
        if "Implementer" in name:
            dispatched.append((prompt_args or {}).get("BRANCH", ""))
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output="")

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
            return AgentIncomplete(
                partial_output='<plan>{"issues": [{"number": 42, "title": "Fix thing"}]}</plan>'
            )
        if "Implementer" in name:
            captured_branches.append((prompt_args or {}).get("BRANCH", ""))
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output="")

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
        if name == "Planner":
            raise PreflightError([("ruff", "ruff check .", "E501")])
        if "preflight-issue" in name:
            return AgentIncomplete(partial_output="<issue>77</issue>")
        if "Implementer" in name:
            captured_branches.append((prompt_args or {}).get("BRANCH", ""))
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output="")

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        github_service=_make_github_svc_afk(),
    )

    assert captured_branches == ["pycastle/issue-77"], (
        f"Expected pycastle/issue-77; got {captured_branches}"
    )


# ── helpers ───────────────────────────────────────────────────────────────────


def _plan_json(issues: list[dict]) -> str:
    return f"<plan>{json.dumps({'issues': issues})}</plan>"


def _make_git_svc(try_merge_side_effect=None, is_ancestor=True):
    mock_svc = MagicMock(spec=GitService)
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
    return mock_svc


def _make_github_svc():
    mock = MagicMock(spec=GithubService)
    mock.has_open_issues_with_label.return_value = True
    mock.get_open_issues.return_value = []
    return mock


def _run(
    tmp_path,
    run_agent_fn,
    *,
    validate_config=None,
    git_service=None,
    github_service=None,
    cfg=None,
):
    asyncio.run(
        run(
            {},
            tmp_path,
            run_agent=run_agent_fn,
            validate_config=validate_config or (lambda _: None),
            git_service=git_service,
            github_service=github_service,
            cfg=cfg if cfg is not None else Config(max_parallel=4, max_iterations=1),
        )
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


# ── Cycle 24-C1/C2: error logging on agent failure ───────────────────────────


def test_failed_agent_appends_traceback_to_errors_log(tmp_path):
    logs_dir = tmp_path / "pycastle" / "logs"
    logs_dir.mkdir(parents=True)
    errors_log = logs_dir / "errors.log"

    boom = RuntimeError("something went wrong")

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return AgentIncomplete(
                partial_output=_plan_json([{"number": 1, "title": "Fix thing"}])
            )
        raise boom

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        cfg=Config(max_parallel=4, max_iterations=1, logs_dir=logs_dir),
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
            return AgentIncomplete(
                partial_output=_plan_json([{"number": 1, "title": "Fix thing"}])
            )
        raise RuntimeError("boom")

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        cfg=Config(max_parallel=4, max_iterations=1, logs_dir=logs_dir),
    )

    assert "---" in errors_log.read_text()


def test_failed_agent_prints_traceback_to_stderr(tmp_path, capsys):
    logs_dir = tmp_path / "pycastle" / "logs"
    logs_dir.mkdir(parents=True)

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return AgentIncomplete(
                partial_output=_plan_json([{"number": 1, "title": "Fix thing"}])
            )
        raise RuntimeError("stderr traceback check")

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        cfg=Config(max_parallel=4, max_iterations=1, logs_dir=logs_dir),
    )

    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert "stderr traceback check" in err


# ── Issue 193: run_issue derives branch from issue number ────────────────────


def test_run_issue_uses_branch_for_when_issue_has_no_branch_key(tmp_path):
    """run_issue must derive the branch via branch_for(number), not read issue['branch']."""
    captured: dict = {}

    async def _fake_run_agent(name, prompt_args=None, branch=None, **kw):
        if "Implementer" in name:
            captured["branch_kwarg"] = branch
            captured["branch_prompt_arg"] = (prompt_args or {}).get("BRANCH")
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output="")

    issue = {"number": 7, "title": "Fix thing"}
    asyncio.run(run_issue(issue, {}, tmp_path, run_agent=_fake_run_agent))

    assert captured["branch_kwarg"] == "pycastle/issue-7"
    assert captured["branch_prompt_arg"] == "pycastle/issue-7"


# ── Cycle 50-4: FEEDBACK_COMMANDS passed to implementer ──────────────────────


def test_run_issue_passes_feedback_commands_to_implementer(tmp_path):
    """run_issue must include FEEDBACK_COMMANDS in prompt_args for the implementer."""
    captured_args: list[dict] = []

    async def _fake_run_agent(
        name, prompt_file, mount_path, env, prompt_args=None, **kw
    ):
        captured_args.append({"name": name, "prompt_args": prompt_args or {}})
        return AgentSuccess(output="<promise>COMPLETE</promise>")

    issue = {"number": 1, "title": "Fix thing"}
    asyncio.run(run_issue(issue, {}, tmp_path, run_agent=_fake_run_agent))

    implementer_call = next(a for a in captured_args if "Implementer" in a["name"])
    assert "FEEDBACK_COMMANDS" in implementer_call["prompt_args"]


def test_run_issue_feedback_commands_formatted_from_implement_checks(tmp_path):
    """FEEDBACK_COMMANDS must be formatted from IMPLEMENT_CHECKS with backtick wrapping."""
    from pycastle.config import IMPLEMENT_CHECKS

    captured_args: list[dict] = []

    async def _fake_run_agent(
        name, prompt_file, mount_path, env, prompt_args=None, **kw
    ):
        captured_args.append({"name": name, "prompt_args": prompt_args or {}})
        return AgentSuccess(output="<promise>COMPLETE</promise>")

    issue = {"number": 1, "title": "Fix thing"}
    asyncio.run(run_issue(issue, {}, tmp_path, run_agent=_fake_run_agent))

    implementer_call = next(a for a in captured_args if "Implementer" in a["name"])
    feedback_commands = implementer_call["prompt_args"]["FEEDBACK_COMMANDS"]
    for cmd in IMPLEMENT_CHECKS:
        assert f"`{cmd}`" in feedback_commands


# ── Cycle 52-1: planner PreflightError → HITL exits ─────────────────────────


def test_planner_preflight_error_spawns_no_implementers(tmp_path):
    """On pre-planning PreflightError with HITL verdict, preflight_phase must exit immediately."""

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            raise PreflightError([("ruff", "ruff check .", "E501 line too long")])
        return AgentIncomplete(partial_output="<issue>77</issue>")

    deps = _make_deps(tmp_path, _fake_run_agent, github_svc=_make_github_svc_hitl())

    with pytest.raises(SystemExit):
        asyncio.run(preflight_phase(deps))


def test_planner_preflight_error_message_names_issue_number(tmp_path, capsys):
    """HITL preflight failure must print a message referencing the filed issue number."""

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            raise PreflightError([("ruff", "ruff check .", "E501 line too long")])
        return AgentIncomplete(partial_output="<issue>88</issue>")

    deps = _make_deps(tmp_path, _fake_run_agent, github_svc=_make_github_svc_hitl())

    with pytest.raises(SystemExit):
        asyncio.run(preflight_phase(deps))

    out = capsys.readouterr().out
    assert "88" in out, f"Output must reference the filed issue number; got: {out!r}"


# ── Cycle 52-2: implementer PreflightFailure → siblings complete ─────────────


def test_implementer_preflight_error_siblings_complete(tmp_path):
    """An implementer PreflightFailure must not prevent sibling issues from completing."""
    completed_issues: list[int] = []

    issues = [
        {"number": 1, "title": "Issue one"},
        {"number": 2, "title": "Issue two"},
    ]

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return AgentIncomplete(partial_output=_plan_json(issues))
        if name == "Implementer #1":
            return PreflightFailure(failures=(("ruff", "ruff check .", "E501"),))
        if "Implementer" in name:
            completed_issues.append(int(name.split("#")[1]))
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output="")

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
            return AgentIncomplete(
                partial_output=_plan_json([{"number": 3, "title": "Fix types"}])
            )
        return PreflightFailure(
            failures=(("mypy", "mypy .", "error: Cannot find module"),)
        )

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        cfg=Config(max_parallel=4, max_iterations=1, logs_dir=logs_dir),
    )

    out = capsys.readouterr().out
    assert "mypy" in out
    assert "mypy ." in out
    assert "[('mypy'" not in out, (
        "Output must not be raw tuple repr — format each check explicitly"
    )


# ── Issue-78: validate_config called at start of run() ───────────────────────


def test_run_calls_validate_config_before_any_agent(tmp_path):
    """validate_config must be called before the first run_agent call."""
    call_order: list[str] = []

    def _fake_validate(overrides):
        call_order.append("validate")

    async def _fake_run_agent(*args, **kwargs):
        call_order.append("agent")
        return AgentIncomplete(partial_output=_plan_json([]))

    _run(
        tmp_path,
        _fake_run_agent,
        validate_config=_fake_validate,
        github_service=_make_github_svc(),
    )

    assert call_order[0] == "validate", f"validate must be first; got {call_order}"


def test_run_validate_config_error_propagates_no_agents_started(tmp_path):
    """ConfigValidationError from validate_config must propagate and prevent all agents."""
    agents_started: list[str] = []

    async def _fake_run_agent(*args, **kwargs):
        agents_started.append(kwargs.get("name", "?"))
        return AgentIncomplete(partial_output="")

    def _raising_validate(_):
        raise ConfigValidationError("bad model")

    with pytest.raises(ConfigValidationError):
        asyncio.run(
            run(
                {},
                tmp_path,
                run_agent=_fake_run_agent,
                validate_config=_raising_validate,
            )
        )

    assert agents_started == [], f"No agents must start; got {agents_started}"


# ── Issue-78: _stage_for_agent helper ─────────────────────────────────────────


def test_stage_for_agent_planner():
    assert _stage_for_agent("Planner") == "plan"


def test_stage_for_agent_implementer():
    assert _stage_for_agent("Implementer #42") == "implement"


def test_stage_for_agent_reviewer():
    assert _stage_for_agent("Reviewer #7") == "review"


def test_stage_for_agent_merger():
    assert _stage_for_agent("Merger") == "merge"


# ── Issue-78: model/effort passed per stage ───────────────────────────────────


def test_planner_receives_plan_stage_model_and_effort(tmp_path):
    """Planner run_agent call must include model and effort from plan stage override."""
    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append(
            {"name": name, "model": kwargs.get("model"), "effort": kwargs.get("effort")}
        )
        return AgentIncomplete(partial_output=_plan_json([]))

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        cfg=Config(
            max_parallel=4,
            max_iterations=1,
            plan_override=StageOverride(model="claude-haiku-4-5", effort="low"),
        ),
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
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(
            partial_output=_plan_json([{"number": 1, "title": "Fix"}])
        )

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(),
        github_service=_make_github_svc(),
        cfg=Config(
            max_parallel=4,
            max_iterations=1,
            implement_override=StageOverride(model="claude-sonnet-4-6", effort="high"),
        ),
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
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(
            partial_output=_plan_json([{"number": 1, "title": "Fix"}])
        )

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(),
        github_service=_make_github_svc(),
        cfg=Config(
            max_parallel=4,
            max_iterations=1,
            review_override=StageOverride(model="claude-haiku-4-5", effort="normal"),
        ),
    )

    rev_call = next(c for c in captured if "Reviewer" in c["name"])
    assert rev_call["model"] == "claude-haiku-4-5"
    assert rev_call["effort"] == "normal"


def test_merger_receives_merge_stage_model_and_effort(tmp_path):
    """Merger run_agent call must include model and effort from merge stage override."""
    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append(
            {"name": name, "model": kwargs.get("model"), "effort": kwargs.get("effort")}
        )
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(
            partial_output=_plan_json([{"number": 1, "title": "Fix"}])
        )

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[False]),
        github_service=_make_github_svc(),
        cfg=Config(
            max_parallel=4,
            max_iterations=1,
            merge_override=StageOverride(model="claude-opus-4-7", effort="low"),
        ),
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
        return AgentIncomplete(partial_output=_plan_json([]))

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
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(
            partial_output=_plan_json([{"number": 1, "title": "Fix"}])
        )

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[False]),
        github_service=_make_github_svc(),
        cfg=Config(
            max_parallel=4,
            max_iterations=1,
            plan_override=StageOverride(model="claude-haiku-4-5", effort="low"),
            implement_override=StageOverride(
                model="claude-sonnet-4-6", effort="normal"
            ),
            review_override=StageOverride(model="claude-haiku-4-5", effort=""),
            merge_override=StageOverride(model="claude-opus-4-7", effort="high"),
        ),
    )

    by_name = {c["name"]: c for c in captured}
    assert by_name["Planner"]["model"] == "claude-haiku-4-5"
    assert by_name["Planner"]["effort"] == "low"
    assert by_name["Implementer #1"]["model"] == "claude-sonnet-4-6"
    assert by_name["Implementer #1"]["effort"] == "normal"
    assert by_name["Reviewer #1"]["model"] == "claude-haiku-4-5"
    assert by_name["Reviewer #1"]["effort"] == ""
    assert by_name["Merger"]["model"] == "claude-opus-4-7"
    assert by_name["Merger"]["effort"] == "high"


# ── Issue-100: stage parameter and CHECKS prompt arg ─────────────────────────


def test_merger_receives_checks_prompt_arg_from_preflight_checks(tmp_path):
    """Merger must receive CHECKS built from PREFLIGHT_CHECKS commands joined by ' && '."""
    from pycastle.config import PREFLIGHT_CHECKS

    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append({"name": name, "prompt_args": kwargs.get("prompt_args", {})})
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(
            partial_output=_plan_json([{"number": 1, "title": "Fix"}])
        )

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[False]),
        github_service=_make_github_svc(),
    )

    merger_call = next(c for c in captured if c["name"] == "Merger")
    expected_checks = " && ".join(cmd for _, cmd in PREFLIGHT_CHECKS)
    assert merger_call["prompt_args"]["CHECKS"] == expected_checks


def test_each_agent_passes_correct_stage_string(tmp_path):
    """Planner, Implementer, Reviewer, and Merger must each pass the correct stage= string."""
    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append({"name": name, "stage": kwargs.get("stage")})
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(
            partial_output=_plan_json([{"number": 1, "title": "Fix"}])
        )

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[False]),
        github_service=_make_github_svc(),
    )

    by_name = {c["name"]: c for c in captured}
    assert by_name["Planner"]["stage"] == "pre-planning"
    assert by_name["Implementer #1"]["stage"] == "pre-implementation"
    assert by_name["Reviewer #1"]["stage"] == "pre-review"
    assert by_name["Merger"]["stage"] == "pre-merge"


# ── Issue-95: parallel implementers with bounded concurrency ──────────────────


def test_multiple_implementers_run_in_parallel(tmp_path):
    """With MAX_PARALLEL >= N issues, all N implementers must be active simultaneously."""
    active_implementers: set[str] = set()
    max_concurrent = 0

    async def _fake_run_agent(
        name, prompt_file, mount_path, env, prompt_args=None, **kw
    ):
        nonlocal max_concurrent
        if name == "Planner":
            return AgentIncomplete(
                partial_output=_plan_json(
                    [
                        {"number": i, "title": f"Issue {i}", "branch": f"issue/{i}"}
                        for i in range(1, 4)
                    ]
                )
            )
        if "Implementer" in name:
            active_implementers.add(name)
            max_concurrent = max(max_concurrent, len(active_implementers))
            await asyncio.sleep(0.05)
            active_implementers.discard(name)
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output="")

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(),
        github_service=_make_github_svc(),
        cfg=Config(max_parallel=4, max_iterations=1),
    )

    assert max_concurrent == 3, (
        f"Expected all 3 implementers active simultaneously, max was {max_concurrent}"
    )


def test_concurrent_agents_never_exceed_max_parallel(tmp_path):
    """The total number of concurrently active agents must never exceed MAX_PARALLEL."""
    active_count = 0
    max_active = 0
    max_parallel = 3

    async def _fake_run_agent(
        name, prompt_file, mount_path, env, prompt_args=None, **kw
    ):
        nonlocal active_count, max_active
        if name == "Planner":
            return AgentIncomplete(
                partial_output=_plan_json(
                    [
                        {"number": i, "title": f"Issue {i}", "branch": f"issue/{i}"}
                        for i in range(1, 8)
                    ]
                )
            )
        active_count += 1
        max_active = max(max_active, active_count)
        await asyncio.sleep(0.01)
        active_count -= 1
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output="")

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(),
        github_service=_make_github_svc(),
        cfg=Config(max_parallel=max_parallel, max_iterations=1),
    )

    assert max_active <= max_parallel, (
        f"Active agents exceeded MAX_PARALLEL={max_parallel}: max observed={max_active}"
    )


def test_implementer_starts_while_reviewer_runs(tmp_path):
    """A new Implementer must be able to start while a prior issue's Reviewer is running."""
    events: list[str] = []

    async def _fake_run_agent(
        name, prompt_file, mount_path, env, prompt_args=None, **kw
    ):
        if name == "Planner":
            return AgentIncomplete(
                partial_output=_plan_json(
                    [
                        {"number": i, "title": f"Issue {i}", "branch": f"issue/{i}"}
                        for i in range(1, 4)
                    ]
                )
            )
        events.append(f"start:{name}")
        await asyncio.sleep(0.03)
        events.append(f"end:{name}")
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output="")

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(),
        github_service=_make_github_svc(),
        cfg=Config(max_parallel=2, max_iterations=1),
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
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output=_plan_json(issues))

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
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output=_plan_json(issues))

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
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output=_plan_json(issues))

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
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output=_plan_json(issues))

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


# ── Issue-190: merge prompt must not contain close-issues instructions ────────


def test_merge_prompt_has_no_close_issues_section():
    """The merge prompt must not instruct the Merger to close GitHub issues."""
    from pycastle.config import PROMPTS_DIR

    merge_prompt = (PROMPTS_DIR / "merge-prompt.md").read_text()
    assert "CLOSE ISSUES" not in merge_prompt, (
        "merge-prompt.md must not contain a CLOSE ISSUES section"
    )


def test_merge_prompt_has_no_issues_placeholder():
    """The merge prompt must not reference the {{ISSUES}} variable."""
    from pycastle.config import PROMPTS_DIR

    merge_prompt = (PROMPTS_DIR / "merge-prompt.md").read_text()
    assert "{{ISSUES}}" not in merge_prompt, (
        "merge-prompt.md must not contain the {{ISSUES}} placeholder"
    )


def test_conflict_merge_calls_close_completed_parent_issues(tmp_path):
    """After conflict branches are merged, close_completed_parent_issues must be called once."""
    issues = [{"number": 5, "title": "Conflict"}]

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output=_plan_json(issues))

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
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output=_plan_json(issues))

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
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output=_plan_json(issues))

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
        if name == "Planner":
            raise PreflightError(
                [("pytest", "pytest -x", "FAILED tests/test_bar.py::test_something")]
            )
        if "preflight-issue" in name:
            return AgentIncomplete(partial_output="<issue>70</issue>")
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output="")

    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            _fake_run_agent,
            github_service=_make_github_svc_hitl(),
        )

    pf_calls = [c for c in captured if "preflight-issue" in c["name"]]
    assert len(pf_calls) == 1
    args = pf_calls[0]["prompt_args"]
    assert args.get("COMMAND") == "pytest -x", (
        f"COMMAND must be 'pytest -x'; got {args.get('COMMAND')!r}"
    )
    assert args.get("OUTPUT") == "FAILED tests/test_bar.py::test_something", (
        f"OUTPUT mismatch; got {args.get('OUTPUT')!r}"
    )


# ── Issue-150: delete_merged_branches ────────────────────────────────────────


def test_delete_merged_branches_deletes_ancestor_branch(tmp_path):
    mock_svc = MagicMock(spec=GitService)
    mock_svc.is_ancestor.return_value = True
    delete_merged_branches(["issue/1"], tmp_path, git_service=mock_svc)
    mock_svc.delete_branch.assert_called_once_with("issue/1", tmp_path)


def test_delete_merged_branches_skips_non_ancestor_branch(tmp_path):
    mock_svc = MagicMock(spec=GitService)
    mock_svc.is_ancestor.return_value = False
    delete_merged_branches(["issue/1"], tmp_path, git_service=mock_svc)
    mock_svc.delete_branch.assert_not_called()


def test_delete_merged_branches_continues_after_git_command_error(tmp_path):
    mock_svc = MagicMock(spec=GitService)
    mock_svc.is_ancestor.return_value = True
    mock_svc.delete_branch.side_effect = [
        GitCommandError("fail", returncode=1, stderr=""),
        None,
    ]
    delete_merged_branches(["issue/1", "issue/2"], tmp_path, git_service=mock_svc)
    assert mock_svc.delete_branch.call_count == 2


def test_delete_merged_branches_prints_warning_to_stderr_on_error(tmp_path, capsys):
    mock_svc = MagicMock(spec=GitService)
    mock_svc.is_ancestor.return_value = True
    mock_svc.delete_branch.side_effect = GitCommandError(
        "fail", returncode=1, stderr=""
    )
    delete_merged_branches(["issue/1"], tmp_path, git_service=mock_svc)
    assert "issue/1" in capsys.readouterr().err


def test_delete_merged_branches_prints_deleted_branch_name(tmp_path, capsys):
    mock_svc = MagicMock(spec=GitService)
    mock_svc.is_ancestor.return_value = True
    delete_merged_branches(["issue/1"], tmp_path, git_service=mock_svc)
    assert "issue/1" in capsys.readouterr().out


def test_delete_merged_branches_uses_injected_git_service(tmp_path):
    mock_svc = MagicMock(spec=GitService)
    mock_svc.is_ancestor.return_value = True
    delete_merged_branches(["issue/1"], tmp_path, git_service=mock_svc)
    mock_svc.is_ancestor.assert_called_once_with("issue/1", tmp_path)


def test_clean_merged_branches_are_deleted_after_try_merge(tmp_path):
    """Branches merged cleanly via try_merge must be deleted after the merge loop."""

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(
            partial_output=_plan_json([{"number": 1, "title": "Fix A"}])
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

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(
            partial_output=_plan_json([{"number": 2, "title": "Conflict"}])
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

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(
            partial_output=_plan_json([{"number": 1, "title": "Fix A"}])
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

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(
            partial_output=_plan_json([{"number": 1, "title": "Fix A"}])
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


# ── run_issue: implementer completion check ───────────────────────────────────


def test_run_issue_returns_none_when_implementer_does_not_complete(tmp_path):
    """run_issue must return None when implementer response lacks <promise>COMPLETE</promise>."""

    async def _fake_run_agent(**kwargs):
        return "I tried but could not finish"

    issue = {"number": 1, "title": "Fix thing"}
    result = asyncio.run(run_issue(issue, {}, tmp_path, run_agent=_fake_run_agent))

    assert result is None


def test_run_issue_returns_issue_when_implementer_completes(tmp_path):
    """run_issue must return the issue dict when implementer produces COMPLETE."""

    async def _fake_run_agent(**kwargs):
        return AgentSuccess(output="<promise>COMPLETE</promise>")

    issue = {"number": 2, "title": "Fix thing"}
    result = asyncio.run(run_issue(issue, {}, tmp_path, run_agent=_fake_run_agent))

    assert result == issue


def test_run_incomplete_implementers_skip_merge(tmp_path):
    """When no implementer produces COMPLETE, try_merge must never be called."""

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return AgentIncomplete(
                partial_output=_plan_json([{"number": 1, "title": "Fix"}])
            )
        return AgentIncomplete(
            partial_output=""
        )  # implementer does not return COMPLETE

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
            return AgentIncomplete(
                partial_output=_plan_json([{"number": 1, "title": "Fix"}])
            )
        raise RuntimeError("agent failed")

    _run(
        tmp_path,
        _fake_run_agent,
        github_service=_make_github_svc(),
        cfg=Config(max_parallel=4, max_iterations=1, logs_dir=logs_dir),
    )

    assert (logs_dir / "errors.log").exists()


# ── Issue-167: dirty-tree polling guard ───────────────────────────────────────


def test_wait_for_clean_working_tree_proceeds_immediately_when_clean(tmp_path):
    """Clean working tree must return without sleeping."""
    import asyncio

    mock_git = MagicMock(spec=GitService)
    mock_git.is_working_tree_clean.return_value = True

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        asyncio.run(wait_for_clean_working_tree(tmp_path, mock_git))

    mock_sleep.assert_not_called()


def test_wait_for_clean_working_tree_prints_no_message_when_clean(tmp_path, capsys):
    """No output must be produced when the tree is already clean."""
    import asyncio

    mock_git = MagicMock(spec=GitService)
    mock_git.is_working_tree_clean.return_value = True

    asyncio.run(wait_for_clean_working_tree(tmp_path, mock_git))

    assert capsys.readouterr().out == ""


def test_wait_for_clean_working_tree_prints_exactly_one_message_when_dirty(
    tmp_path, capsys
):
    """Exactly one non-empty message line must be printed when the tree is dirty."""
    import asyncio

    mock_git = MagicMock(spec=GitService)
    mock_git.is_working_tree_clean.side_effect = [False, False, True]

    with patch("asyncio.sleep", new_callable=AsyncMock):
        asyncio.run(wait_for_clean_working_tree(tmp_path, mock_git))

    non_empty_lines = [
        line for line in capsys.readouterr().out.splitlines() if line.strip()
    ]
    assert len(non_empty_lines) == 1


def test_wait_for_clean_working_tree_polls_every_10_seconds(tmp_path):
    """Each poll cycle must sleep exactly 10 seconds."""
    import asyncio

    mock_git = MagicMock(spec=GitService)
    # Initial check: False; while loop: False → sleep, False → sleep, True → exit
    mock_git.is_working_tree_clean.side_effect = [False, False, False, True]

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        asyncio.run(wait_for_clean_working_tree(tmp_path, mock_git))

    assert mock_sleep.call_count == 2
    assert all(call.args[0] == 10 for call in mock_sleep.call_args_list)


def test_wait_for_clean_working_tree_proceeds_once_clean(tmp_path):
    """Must stop polling and return as soon as the tree becomes clean."""
    import asyncio

    mock_git = MagicMock(spec=GitService)
    # Initial: False → print; loop: False → sleep, True → exit
    mock_git.is_working_tree_clean.side_effect = [False, False, True]

    sleep_calls = []

    async def _fake_sleep(n):
        sleep_calls.append(n)

    with patch("asyncio.sleep", side_effect=_fake_sleep):
        asyncio.run(wait_for_clean_working_tree(tmp_path, mock_git))

    assert sleep_calls == [10]


def test_run_calls_wait_for_clean_working_tree_before_try_merge(tmp_path):
    """wait_for_clean_working_tree must be called before any try_merge call."""
    call_order: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(
            partial_output=_plan_json([{"number": 1, "title": "Fix"}])
        )

    mock_git = _make_git_svc(try_merge_side_effect=[True])
    original_try_merge = mock_git.try_merge.side_effect

    def _tracking_try_merge(repo_path, branch):
        call_order.append("try_merge")
        return original_try_merge(repo_path, branch)

    mock_git.try_merge.side_effect = _tracking_try_merge

    async def _fake_wait(repo_root, git_svc):
        call_order.append("wait")

    with patch(
        "pycastle.orchestrator.wait_for_clean_working_tree", side_effect=_fake_wait
    ):
        _run(
            tmp_path,
            _fake_run_agent,
            git_service=mock_git,
            github_service=_make_github_svc(),
        )

    assert "wait" in call_order, "wait_for_clean_working_tree must be called"
    assert "try_merge" in call_order, "try_merge must be called"
    assert call_order.index("wait") < call_order.index("try_merge"), (
        f"wait must precede try_merge; order={call_order}"
    )


# ── Issue-175: safe SHA pinning and skip-preflight logic ──────────────────────


def test_safe_sha_pinned_and_passed_to_implementer_after_preplanning_preflight(
    tmp_path,
):
    """After pre-planning preflight passes, the HEAD SHA must be captured and passed to implementers."""
    captured_shas: list[str | None] = []
    fake_sha = "deadbeef123"

    mock_git = _make_git_svc(try_merge_side_effect=[True])
    mock_git.get_head_sha.return_value = fake_sha

    async def _fake_run_agent(name, sha=None, **kwargs):
        if "Implementer" in name:
            captured_shas.append(sha)
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(
            partial_output=_plan_json([{"number": 1, "title": "Fix"}])
        )

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
    """On cold startup the Planner must be called exactly once (no issues → terminate)."""
    planner_calls: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            planner_calls.append(name)
            return AgentIncomplete(partial_output=_plan_json([]))
        return AgentIncomplete(partial_output="")

    _run(tmp_path, _fake_run_agent, github_service=_make_github_svc())

    assert len(planner_calls) == 1, f"Expected 1 Planner call; got {len(planner_calls)}"


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
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output=_plan_json(issues))

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


def _make_github_svc_afk():
    """GithubService mock that returns AFK verdict (ready-for-agent) for any issue."""
    mock = MagicMock(spec=GithubService)
    mock.get_labels.return_value = ["bug", "ready-for-agent"]
    mock.get_issue_title.return_value = "Preflight fix title"
    return mock


def _make_github_svc_hitl():
    """GithubService mock that returns HITL verdict (ready-for-human) for any issue."""
    mock = MagicMock(spec=GithubService)
    mock.get_labels.return_value = ["bug", "ready-for-human"]
    mock.get_issue_title.return_value = "Preflight fix title"
    return mock


def test_preflight_failure_afk_planner_skipped_one_implementer(tmp_path):
    """On pre-planning preflight failure with AFK verdict, Planner must NOT be called again
    and exactly one Implementer must be spawned for the preflight issue."""
    agent_names: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        agent_names.append(name)
        if name == "Planner":
            raise PreflightError([("ruff", "ruff check .", "E501 line too long")])
        if "preflight-issue" in name:
            return AgentIncomplete(partial_output="<issue>42</issue>")
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output="")

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        github_service=_make_github_svc_afk(),
    )

    planner_calls = [n for n in agent_names if n == "Planner"]
    implementer_calls = [n for n in agent_names if "Implementer" in n]
    assert len(planner_calls) == 1, (
        f"Planner called once (then errors); got {planner_calls}"
    )
    assert len(implementer_calls) == 1, (
        f"Exactly one Implementer must be spawned for the preflight fix; got {implementer_calls}"
    )


def test_preflight_failure_hitl_exits_nonzero_no_implementer(tmp_path):
    """On pre-planning preflight failure with HITL verdict, process must exit non-zero
    and no Implementer must be spawned."""
    implementer_calls: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            raise PreflightError([("ruff", "ruff check .", "E501")])
        if "preflight-issue" in name:
            return AgentIncomplete(partial_output="<issue>99</issue>")
        if "Implementer" in name:
            implementer_calls.append(name)
        return AgentIncomplete(partial_output="")

    with pytest.raises(SystemExit) as exc_info:
        _run(
            tmp_path,
            _fake_run_agent,
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
        if name == "Planner":
            raise PreflightError(
                [
                    ("ruff", "ruff check .", "ruff error"),
                    ("mypy", "mypy .", "mypy error"),
                    ("pytest", "pytest", "pytest error"),
                ]
            )
        if "preflight-issue" in name:
            preflight_issue_calls.append(
                {"name": name, "prompt_args": prompt_args or {}}
            )
            return AgentIncomplete(partial_output="<issue>10</issue>")
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output="")

    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            _fake_run_agent,
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


# ── Issue-187: implementer and reviewer skip preflight ───────────────────────


def test_implementer_invoked_with_skip_preflight_true(tmp_path):
    """run_issue must pass skip_preflight=True to the implementer agent."""
    captured: list[dict] = []

    async def _fake_run_agent(name, skip_preflight=False, **kwargs):
        captured.append({"name": name, "skip_preflight": skip_preflight})
        return AgentSuccess(output="<promise>COMPLETE</promise>")

    issue = {"number": 1, "title": "Fix thing"}
    asyncio.run(run_issue(issue, {}, tmp_path, run_agent=_fake_run_agent))

    impl_call = next(c for c in captured if "Implementer" in c["name"])
    assert impl_call["skip_preflight"] is True, (
        f"Implementer must receive skip_preflight=True; got {impl_call['skip_preflight']!r}"
    )


def test_reviewer_invoked_with_skip_preflight_true(tmp_path):
    """run_issue must pass skip_preflight=True to the reviewer agent."""
    captured: list[dict] = []

    async def _fake_run_agent(name, skip_preflight=False, **kwargs):
        captured.append({"name": name, "skip_preflight": skip_preflight})
        return AgentSuccess(output="<promise>COMPLETE</promise>")

    issue = {"number": 1, "title": "Fix thing"}
    asyncio.run(run_issue(issue, {}, tmp_path, run_agent=_fake_run_agent))

    rev_call = next(c for c in captured if "Reviewer" in c["name"])
    assert rev_call["skip_preflight"] is True, (
        f"Reviewer must receive skip_preflight=True; got {rev_call['skip_preflight']!r}"
    )


# ── Issue-183: orchestrator exit handling for usage-limit shutdown ─────────────


def test_usage_limit_error_exits_with_code_1(tmp_path):
    """When UsageLimitError is raised by an agent task, orchestrator must exit with code 1."""

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return AgentIncomplete(
                partial_output=_plan_json([{"number": 1, "title": "Fix"}])
            )
        return UsageLimitHit(last_output="")

    with pytest.raises(SystemExit) as exc_info:
        _run(tmp_path, _fake_run_agent, github_service=_make_github_svc())

    assert exc_info.value.code == 1


def test_usage_limit_error_prints_resume_message_to_stderr(tmp_path, capsys):
    """When UsageLimitError is raised by an agent task, the resume message must be printed to stderr."""

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return AgentIncomplete(
                partial_output=_plan_json([{"number": 1, "title": "Fix"}])
            )
        return UsageLimitHit(last_output="")

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
            return AgentIncomplete(
                partial_output=_plan_json([{"number": 1, "title": "Fix"}])
            )
        return UsageLimitHit(last_output="")

    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            _fake_run_agent,
            github_service=_make_github_svc(),
            cfg=Config(max_parallel=4, max_iterations=1, logs_dir=logs_dir),
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
            return AgentIncomplete(
                partial_output=_plan_json(
                    [{"number": 1, "title": "Limit"}, {"number": 2, "title": "Other"}]
                )
            )
        if "Implementer #1" in name:
            return UsageLimitHit(last_output="")
        if "Implementer #2" in name:
            raise RuntimeError("unrelated failure")

    with pytest.raises(SystemExit) as exc_info:
        _run(
            tmp_path,
            _fake_run_agent,
            github_service=_make_github_svc(),
            cfg=Config(max_parallel=4, max_iterations=1),
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
        return AgentIncomplete(partial_output="")

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
        return AgentIncomplete(partial_output="")

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
            return AgentIncomplete(
                partial_output=_plan_json([{"number": 1, "title": "Do thing"}])
            )
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output="")

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
    """run() must pass OPEN_ISSUES_JSON (not ISSUE_LABEL) in planner prompt_args.

    The value must have stale blocker references stripped: an issue body containing
    'Blocked by #99' where #99 is absent from the open issues list must not appear
    in the serialised JSON passed to the planner.
    """
    captured_planner_args: dict = {}

    async def _fake_run_agent(name, prompt_args=None, **kwargs):
        if name == "Planner":
            captured_planner_args.update(prompt_args or {})
            return AgentIncomplete(
                partial_output=_plan_json([{"number": 1, "title": "Fix"}])
            )
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output="")

    mock_github = _make_github_svc()
    mock_github.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Fix thing",
            "body": "Blocked by #99\nDo the work.",
            "labels": [],
            "comments": [],
        }
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
                return AgentIncomplete(
                    partial_output=_plan_json([{"number": 1, "title": "Fix"}])
                )
            return AgentIncomplete(partial_output=_plan_json([]))
        return AgentIncomplete(partial_output="")

    asyncio.run(
        run(
            {},
            tmp_path,
            cfg=Config(max_iterations=2, max_parallel=4),
            run_agent=_fake_run_agent,
            validate_config=lambda _: None,
            github_service=_make_github_svc(),
        )
    )

    assert planner_calls[0] == 2, f"Expected 2 planner calls; got {planner_calls[0]}"


def test_run_limits_concurrency_to_max_parallel_from_cfg(tmp_path):
    """run() with cfg=Config(max_parallel=2) must not exceed 2 concurrent implementers."""
    active_count = 0
    max_active = 0

    async def _fake_run_agent(name, **kwargs):
        nonlocal active_count, max_active
        if name == "Planner":
            return AgentIncomplete(
                partial_output=_plan_json(
                    [{"number": i, "title": f"Issue {i}"} for i in range(1, 6)]
                )
            )
        active_count += 1
        max_active = max(max_active, active_count)
        await asyncio.sleep(0.01)
        active_count -= 1
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output="")

    asyncio.run(
        run(
            {},
            tmp_path,
            cfg=Config(max_parallel=2, max_iterations=1),
            run_agent=_fake_run_agent,
            validate_config=lambda _: None,
            git_service=_make_git_svc(),
            github_service=_make_github_svc(),
        )
    )

    assert max_active <= 2, f"Expected at most 2 concurrent; max was {max_active}"


def test_run_with_no_cfg_completes_using_module_singleton(tmp_path):
    """run() with no cfg argument must complete using the module singleton without error."""

    async def _fake_run_agent(name, **kwargs):
        return AgentIncomplete(partial_output=_plan_json([]))

    asyncio.run(
        run(
            {},
            tmp_path,
            run_agent=_fake_run_agent,
            validate_config=lambda _: None,
            github_service=_make_github_svc(),
        )
    )


def test_run_passes_plan_override_model_and_effort_from_cfg(tmp_path):
    """run() with cfg.plan_override must pass its model and effort to the Planner agent."""
    captured_planner: dict = {}

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            captured_planner["model"] = kwargs.get("model")
            captured_planner["effort"] = kwargs.get("effort")
            return AgentIncomplete(partial_output=_plan_json([]))
        return AgentIncomplete(partial_output="")

    asyncio.run(
        run(
            {},
            tmp_path,
            cfg=Config(
                max_iterations=1,
                max_parallel=4,
                plan_override=StageOverride(model="claude-haiku-4-5", effort="low"),
            ),
            run_agent=_fake_run_agent,
            validate_config=lambda _: None,
            github_service=_make_github_svc(),
        )
    )

    assert captured_planner.get("model") == "claude-haiku-4-5"
    assert captured_planner.get("effort") == "low"


def test_run_applies_validate_config_model_resolution_to_agent_calls(tmp_path):
    """Model resolution from validate_config must reach the agent, not the raw cfg value.

    validate_config mutates the overrides dict in-place (e.g. "haiku" → "claude-haiku-4-5").
    The resolved values must be propagated back into cfg before any agent is called.
    """
    captured_model: list[str] = []

    def _resolving_validate(overrides):
        if overrides.get("plan", {}).get("model") == "haiku":
            overrides["plan"]["model"] = "claude-haiku-4-5"

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            captured_model.append(kwargs.get("model", ""))
            return AgentIncomplete(partial_output=_plan_json([]))
        return AgentIncomplete(partial_output="")

    asyncio.run(
        run(
            {},
            tmp_path,
            cfg=Config(
                max_iterations=1,
                max_parallel=4,
                plan_override=StageOverride(model="haiku"),
            ),
            run_agent=_fake_run_agent,
            validate_config=_resolving_validate,
            github_service=_make_github_svc(),
        )
    )

    assert captured_model == ["claude-haiku-4-5"], (
        "validate_config model resolution must be propagated to the agent call"
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
            return AgentIncomplete(
                partial_output=_plan_json([{"number": 1, "title": "Fix"}])
            )
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output="")

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
                return AgentIncomplete(
                    partial_output=_plan_json([{"number": 1, "title": "Fix"}])
                )
            return AgentIncomplete(partial_output=_plan_json([]))
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output="")

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=mock_git,
        github_service=_make_github_svc(),
        cfg=Config(max_parallel=4, max_iterations=2),
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


# ── Issue-207: preflight_phase boundary tests ─────────────────────────────────


def _make_deps(tmp_path, run_agent_fn, *, git_svc=None, github_svc=None, cfg=None):
    mock_git = git_svc or MagicMock(spec=GitService)
    if git_svc is None:
        mock_git.get_head_sha.return_value = "defaultsha"
    return Deps(
        env={},
        repo_root=tmp_path,
        git_svc=mock_git,
        github_svc=github_svc or _make_github_svc_afk(),
        run_agent=run_agent_fn,
        cfg=cfg or Config(max_parallel=4, max_iterations=1),
    )


def test_preflight_phase_captures_sha_before_checks(tmp_path):
    """preflight_phase must capture HEAD SHA into IterationState.worktree_sha before running checks."""
    fake_sha = "deadbeef123"
    mock_git = MagicMock(spec=GitService)
    mock_git.get_head_sha.return_value = fake_sha

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            raise PreflightError([("ruff", "ruff check .", "E501")])
        return AgentIncomplete(partial_output="<issue>42</issue>")

    deps = _make_deps(
        tmp_path, _fake_run_agent, git_svc=mock_git, github_svc=_make_github_svc_afk()
    )
    state = asyncio.run(preflight_phase(deps))

    assert state.worktree_sha == fake_sha


def test_preflight_phase_failure_spawns_fix_and_preserves_sha(tmp_path):
    """On AFK preflight failure, preflight_phase must spawn a fix issue and preserve worktree_sha."""
    fix_agents_spawned: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            raise PreflightError([("ruff", "ruff check .", "E501")])
        fix_agents_spawned.append(name)
        return AgentIncomplete(partial_output="<issue>77</issue>")

    deps = _make_deps(tmp_path, _fake_run_agent, github_svc=_make_github_svc_afk())
    state = asyncio.run(preflight_phase(deps))

    assert any("preflight-issue" in n for n in fix_agents_spawned), (
        f"Fix-issue agent must be spawned; got {fix_agents_spawned}"
    )
    assert state.worktree_sha is not None


def test_preflight_phase_hitl_exits(tmp_path):
    """On HITL preflight failure, preflight_phase must raise SystemExit."""

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            raise PreflightError([("ruff", "ruff check .", "E501")])
        return AgentIncomplete(partial_output="<issue>88</issue>")

    deps = _make_deps(tmp_path, _fake_run_agent, github_svc=_make_github_svc_hitl())

    with pytest.raises(SystemExit):
        asyncio.run(preflight_phase(deps))


def test_preflight_phase_afk_issues_populated(tmp_path):
    """On AFK preflight failure, state.issues must contain the fix issue number and title."""

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            raise PreflightError([("ruff", "ruff check .", "E501")])
        return AgentIncomplete(partial_output="<issue>77</issue>")

    github_svc = _make_github_svc_afk()
    deps = _make_deps(tmp_path, _fake_run_agent, github_svc=github_svc)
    state = asyncio.run(preflight_phase(deps))

    assert state.issues == [{"number": 77, "title": "Preflight fix title"}]


def test_preflight_phase_success_sets_issues(tmp_path):
    """On success, preflight_phase must return state.issues populated with parsed plan issues."""
    expected = [{"number": 5, "title": "Do thing"}]

    async def _fake_run_agent(name, **kwargs):
        return AgentIncomplete(partial_output=_plan_json(expected))

    deps = _make_deps(tmp_path, _fake_run_agent, github_svc=_make_github_svc())
    state = asyncio.run(preflight_phase(deps))

    assert state.issues == expected
    assert state.worktree_sha == "defaultsha"


# ── Issue-208: plan_phase ─────────────────────────────────────────────────────


def test_plan_phase_success_returns_parsed_issues(tmp_path):
    """plan_phase must return PlanResult with parsed issues from the Planner output."""
    expected = [{"number": 1, "title": "Fix A"}, {"number": 2, "title": "Fix B"}]
    plan_json_output = f"<plan>{json.dumps({'issues': expected})}</plan>"

    async def _fake_run_agent(name, **kwargs):
        return AgentIncomplete(partial_output=plan_json_output)

    deps = _make_deps(tmp_path, _fake_run_agent, github_svc=_make_github_svc())
    state = IterationState(worktree_sha="sha123")
    result = asyncio.run(plan_phase(state, deps))

    assert result.issues == expected


def test_plan_phase_passes_open_issues_json_with_stale_blocker_refs_stripped(tmp_path):
    """plan_phase must pass OPEN_ISSUES_JSON to the Planner with stale blocker refs stripped."""
    open_issues = [
        {"number": 10, "title": "Issue", "body": "Blocked by #99\nOther content"}
    ]
    github_svc = _make_github_svc()
    github_svc.get_open_issues.return_value = open_issues

    captured: dict = {}

    async def _fake_run_agent(name, prompt_args=None, **kwargs):
        captured["prompt_args"] = prompt_args or {}
        return AgentIncomplete(partial_output='<plan>{"issues": []}</plan>')

    deps = _make_deps(tmp_path, _fake_run_agent, github_svc=github_svc)
    state = IterationState(worktree_sha="sha123")
    asyncio.run(plan_phase(state, deps))

    received = json.loads(captured["prompt_args"]["OPEN_ISSUES_JSON"])
    assert received[0]["body"] == "Other content"


def test_plan_phase_raises_when_no_plan_tag(tmp_path):
    """plan_phase must raise RuntimeError when Planner output contains no <plan> tag."""

    async def _fake_run_agent(name, **kwargs):
        return AgentIncomplete(partial_output="no plan tag in this output")

    deps = _make_deps(tmp_path, _fake_run_agent, github_svc=_make_github_svc())
    state = IterationState(worktree_sha="sha123")

    with pytest.raises(RuntimeError, match="no <plan> tag"):
        asyncio.run(plan_phase(state, deps))


# ── Issue-209: implement_phase boundary tests ─────────────────────────────────


def test_implement_phase_returns_completed_issues(tmp_path):
    """implement_phase returns all issues in completed when every run_agent returns COMPLETE."""
    issues = [{"number": 1, "title": "Fix A"}, {"number": 2, "title": "Fix B"}]

    async def _fake_run_agent(name, **kwargs):
        return AgentSuccess(output="<promise>COMPLETE</promise>")

    deps = _make_deps(tmp_path, _fake_run_agent)
    state = IterationState(worktree_sha="abc123")
    result = asyncio.run(implement_phase(issues, state, deps))

    assert result.completed == issues
    assert result.errors == []


def test_implement_phase_usage_limit_propagates(tmp_path):
    """implement_phase must propagate UsageLimitError — not swallow it into errors."""

    issues = [{"number": 1, "title": "Fix A"}]

    async def _fake_run_agent(name, **kwargs):
        return UsageLimitHit(last_output="")

    deps = _make_deps(tmp_path, _fake_run_agent)
    state = IterationState(worktree_sha="abc123")

    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(implement_phase(issues, state, deps))
    assert exc_info.value.code == 1


def test_implement_phase_preflight_failure_goes_to_errors(tmp_path):
    """implement_phase must put PreflightFailure returned by run_agent into result.errors."""
    issues = [{"number": 1, "title": "Fix A"}]

    async def _fake_run_agent(name, **kwargs):
        return PreflightFailure(failures=(("mypy", "mypy .", "error: missing module"),))

    deps = _make_deps(tmp_path, _fake_run_agent)
    state = IterationState(worktree_sha="abc123")
    result = asyncio.run(implement_phase(issues, state, deps))

    assert result.completed == []
    assert len(result.errors) == 1
    assert result.errors[0][0] == issues[0]
    assert isinstance(result.errors[0][1], PreflightFailure)


def test_implement_phase_partial_completion(tmp_path):
    """implement_phase splits results: one completed, one generic exception in errors."""
    issues = [{"number": 1, "title": "Fix A"}, {"number": 2, "title": "Fix B"}]

    async def _fake_run_agent(name, **kwargs):
        if "Implementer #1" in name or "Reviewer #1" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        raise RuntimeError("agent failed")

    deps = _make_deps(tmp_path, _fake_run_agent)
    state = IterationState(worktree_sha="abc123")
    result = asyncio.run(implement_phase(issues, state, deps))

    assert result.completed == [issues[0]]
    assert len(result.errors) == 1
    assert result.errors[0][0] == issues[1]
    assert isinstance(result.errors[0][1], RuntimeError)


def test_implement_phase_usage_limit_awaits_siblings(tmp_path):
    """When one issue raises UsageLimitError, sibling tasks must complete before the error propagates."""

    completed_agents: list[str] = []
    issues = [{"number": 1, "title": "Fail"}, {"number": 2, "title": "Pass"}]

    async def _fake_run_agent(name, **kwargs):
        if "Implementer #1" in name:
            return UsageLimitHit(last_output="")
        completed_agents.append(name)
        return AgentSuccess(output="<promise>COMPLETE</promise>")

    deps = _make_deps(tmp_path, _fake_run_agent)
    state = IterationState(worktree_sha="abc123")

    with pytest.raises(SystemExit):
        asyncio.run(implement_phase(issues, state, deps))

    assert any("Implementer #2" in n for n in completed_agents), (
        f"Sibling Implementer #2 must complete before error propagates; completed={completed_agents}"
    )


def test_implement_phase_no_complete_tag_not_in_completed_or_errors(tmp_path):
    """When run_issue returns None (implementer gave no COMPLETE tag), issue is dropped from both lists."""
    issues = [{"number": 1, "title": "Fix A"}]

    async def _fake_run_agent(name, **kwargs):
        return "some response without the complete tag"

    deps = _make_deps(tmp_path, _fake_run_agent)
    state = IterationState(worktree_sha="abc123")
    result = asyncio.run(implement_phase(issues, state, deps))

    assert result.completed == []
    assert result.errors == []


# ── Issue-210: merge_phase boundary tests ─────────────────────────────────────


def test_merge_phase_clean_merge_closes_issue(tmp_path):
    """merge_phase with a clean try_merge must close the issue and include it in result.clean."""
    issue = {"number": 1, "title": "Fix thing"}
    mock_github = _make_github_svc()
    mock_git = _make_git_svc(try_merge_side_effect=[True])

    deps = Deps(
        env={},
        repo_root=tmp_path,
        git_svc=mock_git,
        github_svc=mock_github,
        run_agent=AsyncMock(return_value=""),
        cfg=Config(max_parallel=4, max_iterations=1),
    )
    result = asyncio.run(merge_phase([issue], deps))

    mock_github.close_issue.assert_called_once_with(1)
    assert result.clean == [issue]
    assert result.conflicts == []


def test_merge_phase_conflict_spawns_merger_agent_and_populates_conflicts(tmp_path):
    """merge_phase with a conflicting try_merge must spawn Merger and put issue in result.conflicts."""
    issue = {"number": 2, "title": "Conflict thing"}
    mock_github = _make_github_svc()
    mock_git = _make_git_svc(try_merge_side_effect=[False])
    merger_calls: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        merger_calls.append(name)
        return AgentIncomplete(partial_output="")

    deps = Deps(
        env={},
        repo_root=tmp_path,
        git_svc=mock_git,
        github_svc=mock_github,
        run_agent=_fake_run_agent,
        cfg=Config(max_parallel=4, max_iterations=1),
    )
    result = asyncio.run(merge_phase([issue], deps))

    assert "Merger" in merger_calls
    assert result.conflicts == [issue]
    assert result.clean == []


def test_merge_phase_deletes_clean_branches(tmp_path):
    """merge_phase must call delete_merged_branches with clean branch names."""
    issue = {"number": 3, "title": "Clean"}
    mock_git = _make_git_svc(try_merge_side_effect=[True], is_ancestor=True)

    deps = Deps(
        env={},
        repo_root=tmp_path,
        git_svc=mock_git,
        github_svc=_make_github_svc(),
        run_agent=AsyncMock(return_value=""),
        cfg=Config(max_parallel=4, max_iterations=1),
    )
    asyncio.run(merge_phase([issue], deps))

    mock_git.delete_branch.assert_called_with("pycastle/issue-3", tmp_path)


def test_merge_phase_clean_merge_calls_close_completed_parent_issues(tmp_path):
    """merge_phase must call close_completed_parent_issues when at least one issue merges cleanly."""
    issue = {"number": 4, "title": "Clean parent"}
    mock_github = _make_github_svc()
    mock_git = _make_git_svc(try_merge_side_effect=[True])

    deps = Deps(
        env={},
        repo_root=tmp_path,
        git_svc=mock_git,
        github_svc=mock_github,
        run_agent=AsyncMock(return_value=""),
        cfg=Config(max_parallel=4, max_iterations=1),
    )
    asyncio.run(merge_phase([issue], deps))

    mock_github.close_completed_parent_issues.assert_called_once()


def test_merge_phase_all_conflicts_calls_close_completed_parent_issues_via_conflict_path(
    tmp_path,
):
    """All-conflict path must still call close_completed_parent_issues after the Merger runs."""
    issue = {"number": 5, "title": "All conflict"}
    mock_github = _make_github_svc()
    mock_git = _make_git_svc(try_merge_side_effect=[False])

    deps = Deps(
        env={},
        repo_root=tmp_path,
        git_svc=mock_git,
        github_svc=mock_github,
        run_agent=AsyncMock(return_value=""),
        cfg=Config(max_parallel=4, max_iterations=1),
    )
    asyncio.run(merge_phase([issue], deps))

    mock_github.close_completed_parent_issues.assert_called_once()
    mock_github.close_issue.assert_called_once_with(5)


def test_merge_phase_conflict_closes_issue_after_merger(tmp_path):
    """merge_phase must call close_issue for each conflict issue after the Merger agent runs."""
    issue = {"number": 6, "title": "Conflict close"}
    mock_github = _make_github_svc()
    mock_git = _make_git_svc(try_merge_side_effect=[False])
    agent_order: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        agent_order.append(name)
        return AgentIncomplete(partial_output="")

    deps = Deps(
        env={},
        repo_root=tmp_path,
        git_svc=mock_git,
        github_svc=mock_github,
        run_agent=_fake_run_agent,
        cfg=Config(max_parallel=4, max_iterations=1),
    )
    asyncio.run(merge_phase([issue], deps))

    assert agent_order == ["Merger"]
    mock_github.close_issue.assert_called_once_with(6)


def test_merge_phase_mixed_partitions_clean_and_conflict(tmp_path):
    """merge_phase with mixed results must partition issues into clean and conflicts correctly."""
    clean_issue = {"number": 7, "title": "Clean"}
    conflict_issue = {"number": 8, "title": "Conflict"}
    mock_github = _make_github_svc()
    mock_git = _make_git_svc(try_merge_side_effect=[True, False])
    merger_calls: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        merger_calls.append(name)
        return AgentIncomplete(partial_output="")

    deps = Deps(
        env={},
        repo_root=tmp_path,
        git_svc=mock_git,
        github_svc=mock_github,
        run_agent=_fake_run_agent,
        cfg=Config(max_parallel=4, max_iterations=1),
    )
    result = asyncio.run(merge_phase([clean_issue, conflict_issue], deps))

    assert result.clean == [clean_issue]
    assert result.conflicts == [conflict_issue]
    assert "Merger" in merger_calls
    mock_github.close_issue.assert_any_call(7)
    mock_github.close_issue.assert_any_call(8)


# ── Issue 226: Merger runs in isolated sandbox worktree ──────────────────────


def test_merge_phase_merger_receives_sandbox_branch(tmp_path):
    """merge_phase must pass branch='pycastle/merge-sandbox' to the Merger's run_agent()."""
    issue = {"number": 9, "title": "Conflict"}
    mock_git = _make_git_svc(try_merge_side_effect=[False])
    captured: dict = {}

    async def _fake_run_agent(name, branch=None, **kwargs):
        if name == "Merger":
            captured["branch"] = branch
        return AgentIncomplete(partial_output="")

    deps = Deps(
        env={},
        repo_root=tmp_path,
        git_svc=mock_git,
        github_svc=_make_github_svc(),
        run_agent=_fake_run_agent,
        cfg=Config(max_parallel=4, max_iterations=1),
    )
    asyncio.run(merge_phase([issue], deps))

    assert captured.get("branch") == MERGE_SANDBOX


def test_merge_phase_merger_mount_path_is_repo_root(tmp_path):
    """merge_phase must pass mount_path=repo_root to Merger so run_agent derives the worktree from it."""
    issue = {"number": 10, "title": "Conflict"}
    mock_git = _make_git_svc(try_merge_side_effect=[False])
    captured: dict = {}

    async def _fake_run_agent(name, mount_path=None, **kwargs):
        if name == "Merger":
            captured["mount_path"] = mount_path
        return AgentIncomplete(partial_output="")

    deps = Deps(
        env={},
        repo_root=tmp_path,
        git_svc=mock_git,
        github_svc=_make_github_svc(),
        run_agent=_fake_run_agent,
        cfg=Config(max_parallel=4, max_iterations=1),
    )
    asyncio.run(merge_phase([issue], deps))

    assert captured.get("mount_path") == tmp_path


# ── Issue-227: Propagate sandbox merge result to target branch ────────────────


def test_merge_phase_fast_forwards_target_branch_after_successful_merger(tmp_path):
    """After Merger succeeds, merge_phase must call fast_forward_branch with MERGE_SANDBOX as source."""
    issue = {"number": 11, "title": "Conflict"}
    mock_git = _make_git_svc(try_merge_side_effect=[False])
    mock_git.get_current_branch.return_value = "main"

    async def _fake_run_agent(name, **kwargs):
        return AgentSuccess(output="")

    deps = Deps(
        env={},
        repo_root=tmp_path,
        git_svc=mock_git,
        github_svc=_make_github_svc(),
        run_agent=_fake_run_agent,
        cfg=Config(max_parallel=4, max_iterations=1),
    )
    asyncio.run(merge_phase([issue], deps))

    mock_git.fast_forward_branch.assert_called_once_with(
        tmp_path, "main", MERGE_SANDBOX
    )


def test_merge_phase_deletes_sandbox_branch_after_successful_fast_forward(tmp_path):
    """After a successful fast-forward, merge_phase must delete the MERGE_SANDBOX branch."""
    issue = {"number": 12, "title": "Conflict"}
    mock_git = _make_git_svc(try_merge_side_effect=[False])
    mock_git.get_current_branch.return_value = "main"

    async def _fake_run_agent(name, **kwargs):
        return AgentSuccess(output="")

    deps = Deps(
        env={},
        repo_root=tmp_path,
        git_svc=mock_git,
        github_svc=_make_github_svc(),
        run_agent=_fake_run_agent,
        cfg=Config(max_parallel=4, max_iterations=1),
    )
    asyncio.run(merge_phase([issue], deps))

    mock_git.delete_branch.assert_any_call(MERGE_SANDBOX, tmp_path)


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
            return AgentIncomplete(
                partial_output=_plan_json([{"number": 1, "title": "Fix thing"}])
            )
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output="")

    asyncio.run(
        run(
            {},
            git_repo,
            run_agent=_fake_run_agent,
            validate_config=lambda _: None,
            github_service=mock_github,
            cfg=Config(max_parallel=4, max_iterations=1),
        )
    )

    assert 1 in closed_issues, (
        f"Issue #1 must be closed after merge; closed={closed_issues}"
    )


# ── Issue-214: promise check inside run_agent ─────────────────────────────────


def test_complete_check_inside_run_agent_success(tmp_path):
    """run_issue returns the issue when injected run_agent returns AgentSuccess."""

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output="")

    issue = {"number": 1, "title": "Fix thing"}
    result = asyncio.run(run_issue(issue, {}, tmp_path, run_agent=_fake_run_agent))
    assert result == issue


def test_complete_check_inside_run_agent_incomplete(tmp_path):
    """run_issue returns None when injected run_agent returns AgentIncomplete."""

    async def _fake_run_agent(name, **kwargs):
        return AgentIncomplete(partial_output="partial work done")

    issue = {"number": 1, "title": "Fix thing"}
    result = asyncio.run(run_issue(issue, {}, tmp_path, run_agent=_fake_run_agent))
    assert result is None


# ── Issue-215: run_issue reviewer UsageLimitHit propagation ──────────────────


def test_run_issue_returns_usage_limit_hit_when_reviewer_hits_limit(tmp_path):
    """run_issue must return UsageLimitHit when the reviewer (not the implementer) hits the usage limit."""

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return UsageLimitHit(last_output="")

    issue = {"number": 1, "title": "Fix thing"}
    result = asyncio.run(run_issue(issue, {}, tmp_path, run_agent=_fake_run_agent))

    assert isinstance(result, UsageLimitHit)


def test_implement_phase_usage_limit_hit_not_counted_as_completed(tmp_path):
    """UsageLimitHit results must not appear in completed even when sys.exit is patched to not exit."""
    issues = [{"number": 1, "title": "Fix A"}, {"number": 2, "title": "Fix B"}]
    exit_calls: list[int] = []

    async def _fake_run_agent(name, **kwargs):
        if name == "Implementer #1":
            return UsageLimitHit(last_output="")
        return AgentSuccess(output="<promise>COMPLETE</promise>")

    deps = _make_deps(tmp_path, _fake_run_agent)
    state = IterationState(worktree_sha="abc123")

    with patch("pycastle.orchestrator.sys") as mock_sys:
        mock_sys.exit.side_effect = lambda code: exit_calls.append(code)
        mock_sys.stderr = sys.stderr
        result = asyncio.run(implement_phase(issues, state, deps))

    assert exit_calls == [1], "sys.exit(1) must be called"
    assert result.completed == [{"number": 2, "title": "Fix B"}], (
        "Only the non-UsageLimitHit issue must appear in completed"
    )
    assert result.errors == [], "UsageLimitHit must not appear in errors"


# ── Issue-214: IssueNumberParseFailure from _handle_preflight_failure ──────────


def test_handle_preflight_failure_returns_typed_failure_when_no_issue_tag(tmp_path):
    """_handle_preflight_failure returns IssueNumberParseFailure when agent output has no <issue>N</issue> tag."""
    from pycastle.agent_result import IssueNumberParseFailure
    from pycastle.orchestrator import _handle_preflight_failure

    async def _fake_run_agent(**kwargs):
        return AgentIncomplete(partial_output="no issue tag here")

    github_svc = _make_github_svc()
    result = asyncio.run(
        _handle_preflight_failure(
            [("ruff", "ruff check .", "E501")],
            {},
            tmp_path,
            github_svc,
            _fake_run_agent,
            "hitl",
            tmp_path,
        )
    )
    assert isinstance(result, IssueNumberParseFailure)
