import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pycastle.errors import ConfigValidationError, PreflightError
from pycastle.git_service import GitCommandError, GitService
from pycastle.github_service import GithubService
from pycastle.orchestrator import (
    _stage_for_agent,
    branch_for,
    delete_merged_branches,
    parse_plan,
    prune_orphan_worktrees,
    run,
    run_issue,
    strip_stale_blocker_refs,
    wait_for_clean_working_tree,
)


# ── parse_plan ────────────────────────────────────────────────────────────────


def test_parse_plan_returns_issues_list():
    output = '<plan>{"issues": [{"number": 1, "title": "Fix bug"}]}</plan>'
    assert parse_plan(output) == [{"number": 1, "title": "Fix bug"}]


def test_parse_plan_returns_empty_list_when_no_issues():
    output = '<plan>{"issues": []}</plan>'
    assert parse_plan(output) == []


def test_parse_plan_raises_when_no_plan_tag():
    with pytest.raises(RuntimeError, match="no <plan> tag"):
        parse_plan("some output with no plan tag")


def test_parse_plan_returns_unblocked_issues_list():
    output = '<plan>{"unblocked_issues": [{"number": 2, "title": "Do thing"}], "blocked_issues": [{"number": 3, "title": "Later"}]}</plan>'
    assert parse_plan(output) == [{"number": 2, "title": "Do thing"}]


def test_parse_plan_raises_descriptively_when_issues_key_missing():
    output = '<plan>{"something_else": []}</plan>'
    with pytest.raises(
        RuntimeError, match="'unblocked_issues'.*'issues'|'issues'.*'unblocked_issues'"
    ):
        parse_plan(output)


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


def test_strip_stale_blocker_refs_does_not_mutate_input():
    original_body = "Blocked by #99\nKeep this."
    issues = [{"number": 10, "title": "Fix bug", "body": original_body}]
    strip_stale_blocker_refs(issues)
    assert issues[0]["body"] == original_body


# ── branch_for ───────────────────────────────────────────────────────────────


def test_branch_for_returns_sandcastle_issue_format():
    assert branch_for(193) == "sandcastle/issue-193"


def test_branch_for_uses_issue_number():
    assert branch_for(1) == "sandcastle/issue-1"
    assert branch_for(42) == "sandcastle/issue-42"


# ── Issue 193: run() works when planner omits branch field ───────────────────


def test_run_does_not_crash_when_planner_omits_branch_field(tmp_path):
    """run() must not KeyError when planner output has no 'branch' key in issues."""
    dispatched: list[str] = []

    async def _fake_run_agent(name, prompt_args=None, **kwargs):
        if name == "Planner":
            return (
                '<plan>{"issues": [{"number": 193, "title": "Fix branch bug"}]}</plan>'
            )
        if "Implementer" in name:
            dispatched.append((prompt_args or {}).get("BRANCH", ""))
            return "<promise>COMPLETE</promise>"
        return ""

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        github_service=_make_github_svc(),
    )

    assert dispatched == ["sandcastle/issue-193"]


# ── Issue 188: deterministic branch names ────────────────────────────────────


def test_run_computes_branch_from_issue_number_not_planner_slug(tmp_path):
    """After parse_plan, each issue branch must be sandcastle/issue-N, ignoring planner slug."""
    captured_branches: list[str] = []

    async def _fake_run_agent(name, prompt_args=None, **kwargs):
        if name == "Planner":
            return '<plan>{"issues": [{"number": 42, "title": "Fix thing"}]}</plan>'
        if "Implementer" in name:
            captured_branches.append((prompt_args or {}).get("BRANCH", ""))
            return "<promise>COMPLETE</promise>"
        return ""

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        github_service=_make_github_svc(),
    )

    assert captured_branches == ["sandcastle/issue-42"], (
        f"Expected branch sandcastle/issue-42; got {captured_branches}"
    )


def test_preflight_issue_branch_uses_sandcastle_format(tmp_path):
    """A preflight fix issue must use branch sandcastle/issue-N, not issue/N."""
    captured_branches: list[str] = []

    async def _fake_run_agent(name, prompt_args=None, branch=None, **kwargs):
        if name == "Planner":
            raise PreflightError([("ruff", "ruff check .", "E501")])
        if "preflight-issue" in name:
            return "<issue>77</issue>"
        if "Implementer" in name:
            captured_branches.append((prompt_args or {}).get("BRANCH", ""))
            return "<promise>COMPLETE</promise>"
        return ""

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        github_service=_make_github_svc_afk(),
        max_iterations=1,
    )

    assert captured_branches == ["sandcastle/issue-77"], (
        f"Expected sandcastle/issue-77; got {captured_branches}"
    )


def test_post_merge_preflight_fix_tries_merge_on_sandcastle_branch(tmp_path):
    """After a post-merge check failure, try_merge for the preflight fix must use sandcastle/issue-N."""
    check_call_count = [0]
    try_merge_branches: list[str] = []

    mock_git = _make_git_svc()

    def _try_merge(repo_path, branch):
        try_merge_branches.append(branch)
        return True

    mock_git.try_merge.side_effect = _try_merge
    mock_git.get_head_sha.return_value = "sha"

    def _run_host_checks(_):
        check_call_count[0] += 1
        if check_call_count[0] == 1:
            return [("pytest", "pytest", "FAILED")]
        return []

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json([{"number": 1, "title": "Fix"}])
        if "preflight-issue" in name:
            return "<issue>55</issue>"
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return ""

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=mock_git,
        github_service=_make_github_svc_afk(),
        run_host_checks=_run_host_checks,
    )

    pf_merge_calls = [b for b in try_merge_branches if "55" in b]
    assert pf_merge_calls == ["sandcastle/issue-55"], (
        f"Preflight fix must try_merge sandcastle/issue-55; got {pf_merge_calls}"
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
    return mock


def _run(
    tmp_path,
    run_agent_fn,
    *,
    validate_config=None,
    git_service=None,
    github_service=None,
    run_host_checks=None,
    stage_overrides=None,
    max_parallel=4,
    max_iterations=1,
    logs_dir=None,
):
    asyncio.run(
        run(
            {},
            tmp_path,
            run_agent=run_agent_fn,
            validate_config=validate_config or (lambda _: None),
            git_service=git_service,
            github_service=github_service,
            run_host_checks=run_host_checks or (lambda _: []),
            stage_overrides=stage_overrides,
            max_parallel=max_parallel,
            max_iterations=max_iterations,
            logs_dir=logs_dir,
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
            return _plan_json([{"number": 1, "title": "Fix thing"}])
        raise boom

    _run(
        tmp_path, _fake_run_agent, github_service=_make_github_svc(), logs_dir=logs_dir
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
        tmp_path, _fake_run_agent, github_service=_make_github_svc(), logs_dir=logs_dir
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
        tmp_path, _fake_run_agent, github_service=_make_github_svc(), logs_dir=logs_dir
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
            return "<promise>COMPLETE</promise>"
        return ""

    issue = {"number": 7, "title": "Fix thing"}
    asyncio.run(run_issue(issue, {}, tmp_path, run_agent=_fake_run_agent))

    assert captured["branch_kwarg"] == "sandcastle/issue-7"
    assert captured["branch_prompt_arg"] == "sandcastle/issue-7"


# ── Cycle 50-4: FEEDBACK_COMMANDS passed to implementer ──────────────────────


def test_run_issue_passes_feedback_commands_to_implementer(tmp_path):
    """run_issue must include FEEDBACK_COMMANDS in prompt_args for the implementer."""
    captured_args: list[dict] = []

    async def _fake_run_agent(
        name, prompt_file, mount_path, env, prompt_args=None, **kw
    ):
        captured_args.append({"name": name, "prompt_args": prompt_args or {}})
        return "<promise>COMPLETE</promise>"

    issue = {"number": 1, "title": "Fix thing"}
    asyncio.run(run_issue(issue, {}, tmp_path, run_agent=_fake_run_agent))

    implementer_call = next(a for a in captured_args if "Implementer" in a["name"])
    assert "FEEDBACK_COMMANDS" in implementer_call["prompt_args"]


def test_run_issue_feedback_commands_formatted_from_implement_checks(tmp_path):
    """FEEDBACK_COMMANDS must be formatted from IMPLEMENT_CHECKS with backtick wrapping."""
    from pycastle.defaults.config import IMPLEMENT_CHECKS

    captured_args: list[dict] = []

    async def _fake_run_agent(
        name, prompt_file, mount_path, env, prompt_args=None, **kw
    ):
        captured_args.append({"name": name, "prompt_args": prompt_args or {}})
        return "<promise>COMPLETE</promise>"

    issue = {"number": 1, "title": "Fix thing"}
    asyncio.run(run_issue(issue, {}, tmp_path, run_agent=_fake_run_agent))

    implementer_call = next(a for a in captured_args if "Implementer" in a["name"])
    feedback_commands = implementer_call["prompt_args"]["FEEDBACK_COMMANDS"]
    for cmd in IMPLEMENT_CHECKS:
        assert f"`{cmd}`" in feedback_commands


# ── Cycle 52-1: planner PreflightError → no implementers spawned ─────────────


def test_planner_preflight_error_spawns_no_implementers(tmp_path):
    """On pre-planning PreflightError with HITL verdict, no Implementer must be spawned."""
    implementer_names: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            raise PreflightError([("ruff", "ruff check .", "E501 line too long")])
        if "preflight-issue" in name:
            return "<issue>77</issue>"
        implementer_names.append(name)
        return ""

    with pytest.raises(SystemExit):
        _run(tmp_path, _fake_run_agent, github_service=_make_github_svc_hitl())

    assert implementer_names == [], (
        f"Expected no implementers on HITL verdict, got: {implementer_names}"
    )


def test_planner_preflight_error_message_names_issue_number(tmp_path, capsys):
    """HITL preflight failure must print a message referencing the filed issue number."""

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            raise PreflightError([("ruff", "ruff check .", "E501 line too long")])
        if "preflight-issue" in name:
            return "<issue>88</issue>"
        return ""

    with pytest.raises(SystemExit):
        _run(tmp_path, _fake_run_agent, github_service=_make_github_svc_hitl())

    out = capsys.readouterr().out
    assert "88" in out, f"Output must reference the filed issue number; got: {out!r}"


# ── Cycle 52-2: implementer PreflightError → siblings complete ───────────────


def test_implementer_preflight_error_siblings_complete(tmp_path):
    """An implementer PreflightError must not prevent sibling issues from completing."""
    completed_issues: list[int] = []

    issues = [
        {"number": 1, "title": "Issue one"},
        {"number": 2, "title": "Issue two"},
    ]

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json(issues)
        if name == "Implementer #1":
            raise PreflightError([("ruff", "ruff check .", "E501")])
        if "Implementer" in name:
            completed_issues.append(int(name.split("#")[1]))
            return "<promise>COMPLETE</promise>"
        return ""

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
    """An implementer PreflightError must print the failed check name and command to stdout."""

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json([{"number": 3, "title": "Fix types"}])
        raise PreflightError([("mypy", "mypy .", "error: Cannot find module")])

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    _run(
        tmp_path, _fake_run_agent, github_service=_make_github_svc(), logs_dir=logs_dir
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
        return _plan_json([])

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
        return ""

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
        return _plan_json([])

    stage_overrides = {
        "plan": {"model": "claude-haiku-4-5", "effort": "low"},
        "implement": {"model": "", "effort": ""},
        "review": {"model": "", "effort": ""},
        "merge": {"model": "", "effort": ""},
    }

    _run(
        tmp_path,
        _fake_run_agent,
        stage_overrides=stage_overrides,
        github_service=_make_github_svc(),
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

    stage_overrides = {
        "plan": {"model": "", "effort": ""},
        "implement": {"model": "claude-sonnet-4-6", "effort": "high"},
        "review": {"model": "", "effort": ""},
        "merge": {"model": "", "effort": ""},
    }

    _run(
        tmp_path,
        _fake_run_agent,
        stage_overrides=stage_overrides,
        git_service=_make_git_svc(),
        github_service=_make_github_svc(),
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

    stage_overrides = {
        "plan": {"model": "", "effort": ""},
        "implement": {"model": "", "effort": ""},
        "review": {"model": "claude-haiku-4-5", "effort": "normal"},
        "merge": {"model": "", "effort": ""},
    }

    _run(
        tmp_path,
        _fake_run_agent,
        stage_overrides=stage_overrides,
        git_service=_make_git_svc(),
        github_service=_make_github_svc(),
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
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 1, "title": "Fix"}])

    stage_overrides = {
        "plan": {"model": "", "effort": ""},
        "implement": {"model": "", "effort": ""},
        "review": {"model": "", "effort": ""},
        "merge": {"model": "claude-opus-4-7", "effort": "low"},
    }

    _run(
        tmp_path,
        _fake_run_agent,
        stage_overrides=stage_overrides,
        git_service=_make_git_svc(try_merge_side_effect=[False]),
        github_service=_make_github_svc(),
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

    stage_overrides = {
        "plan": {"model": "", "effort": ""},
        "implement": {"model": "", "effort": ""},
        "review": {"model": "", "effort": ""},
        "merge": {"model": "", "effort": ""},
    }

    _run(
        tmp_path,
        _fake_run_agent,
        stage_overrides=stage_overrides,
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

    stage_overrides = {
        "plan": {"model": "claude-haiku-4-5", "effort": "low"},
        "implement": {"model": "claude-sonnet-4-6", "effort": "normal"},
        "review": {"model": "claude-haiku-4-5", "effort": ""},
        "merge": {"model": "claude-opus-4-7", "effort": "high"},
    }

    _run(
        tmp_path,
        _fake_run_agent,
        stage_overrides=stage_overrides,
        git_service=_make_git_svc(try_merge_side_effect=[False]),
        github_service=_make_github_svc(),
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
    from pycastle.defaults.config import PREFLIGHT_CHECKS

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
    expected_checks = " && ".join(cmd for _, cmd in PREFLIGHT_CHECKS)
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
        return ""

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(),
        github_service=_make_github_svc(),
        max_parallel=4,
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
        return ""

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

    async def _fake_run_agent(
        name, prompt_file, mount_path, env, prompt_args=None, **kw
    ):
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
        return ""

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(),
        github_service=_make_github_svc(),
        max_parallel=2,
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
    assert "sandcastle/issue-2" in branches_arg
    assert "sandcastle/issue-1" not in branches_arg


def test_conflict_branch_skips_post_merge_checks(tmp_path):
    """When any branch conflicts, post-merge host checks must not run."""
    host_checks_called = []

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 1, "title": "Conflict"}])

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[False]),
        github_service=_make_github_svc(),
        run_host_checks=lambda _: host_checks_called.append(True) or [],
    )

    assert host_checks_called == [], "Host checks must not run when conflicts exist"


def test_post_merge_checks_run_after_all_clean_merges(tmp_path):
    """After all clean merges with no conflicts, host PREFLIGHT_CHECKS must run."""
    host_checks_called = []

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 1, "title": "Fix"}])

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        github_service=_make_github_svc(),
        run_host_checks=lambda checks: host_checks_called.append(checks) or [],
    )

    assert len(host_checks_called) == 1, (
        f"_run_host_checks must be called once; called {len(host_checks_called)} times"
    )


def test_post_merge_check_failure_spawns_preflight_issue_not_merger(tmp_path):
    """On post-merge check failure, preflight-issue agent is spawned and Merger is NOT spawned."""
    agent_names: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        agent_names.append(name)
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        if "preflight-issue" in name:
            return "<issue>55</issue>"
        return _plan_json([{"number": 1, "title": "Fix"}])

    failures = [("pytest", "pytest", "FAILED tests/test_foo.py")]
    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            _fake_run_agent,
            git_service=_make_git_svc(try_merge_side_effect=[True]),
            github_service=_make_github_svc_hitl(),
            run_host_checks=lambda _: failures,
        )

    assert "Merger" not in agent_names, (
        f"Merger must not spawn on check failure; agents={agent_names}"
    )
    preflight_names = [n for n in agent_names if "preflight-issue" in n]
    assert len(preflight_names) >= 1, (
        f"At least one preflight-issue agent expected; agents={agent_names}"
    )


def test_post_merge_preflight_issue_uses_raw_check_name(tmp_path):
    """preflight-issue agent spawned on post-merge failure must use the raw CHECK_NAME."""
    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append({"name": name, "prompt_args": kwargs.get("prompt_args", {})})
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        if "preflight-issue" in name:
            return "<issue>60</issue>"
        return _plan_json([{"number": 1, "title": "Fix"}])

    failures = [("pytest", "pytest", "FAILED")]
    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            _fake_run_agent,
            git_service=_make_git_svc(try_merge_side_effect=[True]),
            github_service=_make_github_svc_hitl(),
            run_host_checks=lambda _: failures,
        )

    pf_calls = [c for c in captured if "preflight-issue" in c["name"]]
    assert pf_calls, "Expected preflight-issue agent calls"
    check_name = pf_calls[0]["prompt_args"].get("CHECK_NAME", "")
    assert check_name == "pytest", (
        f"CHECK_NAME must be raw check name 'pytest'; got {check_name!r}"
    )


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


def test_post_merge_multiple_check_failures_only_first_acted_on(tmp_path):
    """When multiple post-merge checks fail, only the first must spawn a preflight-issue agent."""
    agent_names: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        agent_names.append(name)
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        if "preflight-issue" in name:
            return "<issue>66</issue>"
        return _plan_json([{"number": 1, "title": "Fix"}])

    failures = [
        ("pytest", "pytest", "FAILED tests/test_foo.py"),
        ("mypy", "mypy .", "error: Found 2 errors"),
        ("ruff", "ruff check .", "ruff: error"),
    ]
    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            _fake_run_agent,
            git_service=_make_git_svc(try_merge_side_effect=[True]),
            github_service=_make_github_svc_hitl(),
            run_host_checks=lambda _: failures,
        )

    pf_agents = [n for n in agent_names if "preflight-issue" in n]
    assert len(pf_agents) == 1, (
        f"Exactly one preflight-issue agent expected; got {pf_agents}"
    )


def test_preflight_issue_receives_correct_command_and_output(tmp_path):
    """preflight-issue agent must receive exact COMMAND and OUTPUT from the failing check."""
    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append({"name": name, "prompt_args": kwargs.get("prompt_args", {})})
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        if "preflight-issue" in name:
            return "<issue>70</issue>"
        return _plan_json([{"number": 1, "title": "Fix"}])

    failures = [("pytest", "pytest -x", "FAILED tests/test_bar.py::test_something")]
    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            _fake_run_agent,
            git_service=_make_git_svc(try_merge_side_effect=[True]),
            github_service=_make_github_svc_hitl(),
            run_host_checks=lambda _: failures,
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
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 1, "title": "Fix A"}])

    mock_git = _make_git_svc(try_merge_side_effect=[True], is_ancestor=True)
    _run(
        tmp_path,
        _fake_run_agent,
        git_service=mock_git,
        github_service=_make_github_svc(),
    )

    mock_git.delete_branch.assert_called_with("sandcastle/issue-1", tmp_path)


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

    mock_git.delete_branch.assert_called_with("sandcastle/issue-2", tmp_path)


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
        return "<promise>COMPLETE</promise>"

    issue = {"number": 2, "title": "Fix thing"}
    result = asyncio.run(run_issue(issue, {}, tmp_path, run_agent=_fake_run_agent))

    assert result == issue


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
        tmp_path, _fake_run_agent, github_service=_make_github_svc(), logs_dir=logs_dir
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
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 1, "title": "Fix"}])

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


def test_safe_sha_repinned_after_passing_post_merge_check(tmp_path):
    """After post-merge checks pass, _safe_sha must be repinned to the new HEAD SHA for next iteration."""
    captured_shas: list[str | None] = []
    shas = ["first_sha", "post_merge_sha", "second_iter_sha"]
    sha_index = [0]

    mock_git = _make_git_svc(try_merge_side_effect=[True, True])

    def _get_head_sha(_repo_root):
        sha = shas[sha_index[0]]
        sha_index[0] += 1
        return sha

    mock_git.get_head_sha.side_effect = _get_head_sha

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
        run_host_checks=lambda _: [],
        max_iterations=2,
    )

    assert len(captured_shas) == 2, f"Expected 2 implementer calls; got {captured_shas}"
    assert captured_shas[0] == "first_sha", (
        f"First implementer must use pre-planning SHA; got {captured_shas[0]!r}"
    )
    assert captured_shas[1] == "post_merge_sha", (
        f"Second implementer must use post-merge repinned SHA; got {captured_shas[1]!r}"
    )


def test_preplanning_preflight_skipped_when_post_merge_check_just_passed(tmp_path):
    """When post-merge check passed in previous iteration, skip_preflight=True must be passed to Planner."""
    planner_skip_flags: list[bool] = []

    mock_git = _make_git_svc(try_merge_side_effect=[True, True])
    mock_git.get_head_sha.return_value = "any_sha"

    async def _fake_run_agent(name, skip_preflight=False, sha=None, **kwargs):
        if name == "Planner":
            planner_skip_flags.append(skip_preflight)
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 1, "title": "Fix"}])

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=mock_git,
        github_service=_make_github_svc(),
        run_host_checks=lambda _: [],
        max_iterations=2,
    )

    assert len(planner_skip_flags) == 2, (
        f"Expected 2 Planner calls; got {len(planner_skip_flags)}"
    )
    assert planner_skip_flags[0] is False, (
        "First iteration must run preflight (cold startup)"
    )
    assert planner_skip_flags[1] is True, (
        "Second iteration must skip preflight when post-merge check just passed"
    )


def test_preplanning_preflight_runs_on_cold_startup(tmp_path):
    """On cold startup (first iteration), skip_preflight must be False for the Planner."""
    planner_calls: list[dict] = []

    async def _fake_run_agent(name, skip_preflight=False, **kwargs):
        if name == "Planner":
            planner_calls.append({"skip_preflight": skip_preflight})
            return _plan_json([])
        return ""

    _run(tmp_path, _fake_run_agent, github_service=_make_github_svc())

    assert len(planner_calls) == 1, f"Expected 1 Planner call; got {len(planner_calls)}"
    assert planner_calls[0]["skip_preflight"] is False, (
        "Planner must not skip preflight on cold startup"
    )


def test_preplanning_preflight_reruns_after_post_merge_check_failure(tmp_path):
    """When the preflight-fix post-merge also fails, the next iteration must run preflight again."""
    planner_skip_flags: list[bool] = []
    planner_call_count = [0]

    mock_git = _make_git_svc(try_merge_side_effect=[True, True])
    mock_git.get_head_sha.return_value = "some_sha"

    mock_github = MagicMock(spec=GithubService)
    mock_github.get_labels.return_value = ["ready-for-agent"]  # AFK verdict
    mock_github.get_issue_title.return_value = "Fix preflight"

    async def _fake_run_agent(name, skip_preflight=False, **kwargs):
        if name == "Planner":
            planner_skip_flags.append(skip_preflight)
            planner_call_count[0] += 1
            if planner_call_count[0] == 1:
                return _plan_json([{"number": 1, "title": "Fix"}])
            return _plan_json([])  # second iteration: no issues → terminate
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        if "preflight-issue" in name:
            return "<issue>90</issue>"
        return ""

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=mock_git,
        github_service=mock_github,
        run_host_checks=lambda _: [("pytest", "pytest", "FAILED")],  # always fails
        max_iterations=2,
    )

    assert len(planner_skip_flags) == 2, (
        f"Expected 2 Planner calls; got {len(planner_skip_flags)}"
    )
    assert planner_skip_flags[0] is False, "First iteration must run preflight"
    assert planner_skip_flags[1] is False, (
        "Second iteration must run preflight again when preflight-fix post-merge also failed"
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
            return "<issue>42</issue>"
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return ""

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        github_service=_make_github_svc_afk(),
        max_iterations=1,
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
            return "<issue>99</issue>"
        if "Implementer" in name:
            implementer_calls.append(name)
        return ""

    with pytest.raises(SystemExit) as exc_info:
        _run(
            tmp_path,
            _fake_run_agent,
            github_service=_make_github_svc_hitl(),
            max_iterations=1,
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
            return "<issue>10</issue>"
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return ""

    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            _fake_run_agent,
            github_service=_make_github_svc_hitl(),
            max_iterations=1,
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


def test_preplanning_and_postmerge_failures_use_same_handler(tmp_path):
    """Both pre-planning and post-merge preflight failures must spawn a preflight-issue agent."""
    preflight_issue_calls: list[str] = []
    labels_call_count = [0]

    mock_git = _make_git_svc(try_merge_side_effect=[True])
    mock_github = MagicMock(spec=GithubService)
    mock_github.get_issue_title.return_value = "Preflight fix"

    def _get_labels(issue_number):
        labels_call_count[0] += 1
        if labels_call_count[0] == 1:
            return ["ready-for-agent"]  # AFK: let pre-planning fix run
        return ["ready-for-human"]  # HITL: exit on post-merge failure

    mock_github.get_labels.side_effect = _get_labels

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            raise PreflightError([("ruff", "ruff check .", "error")])
        if "preflight-issue" in name:
            preflight_issue_calls.append(name)
            return "<issue>50</issue>"
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return ""

    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            _fake_run_agent,
            git_service=mock_git,
            github_service=mock_github,
            run_host_checks=lambda _: [("pytest", "pytest", "FAILED")],
            max_iterations=1,
        )

    assert len(preflight_issue_calls) == 2, (
        f"preflight-issue must be spawned for both pre-planning and post-merge failures; "
        f"got {len(preflight_issue_calls)}"
    )


def test_afk_post_merge_fix_success_skips_preflight_next_iteration(tmp_path):
    """When AFK post-merge fix merges and its second check passes, the next Planner must skip preflight."""
    planner_skip_flags: list[bool] = []
    planner_call_count = [0]
    check_call_count = [0]

    mock_git = _make_git_svc(try_merge_side_effect=[True, True])
    mock_git.get_head_sha.return_value = "fix_sha"

    mock_github = MagicMock(spec=GithubService)
    mock_github.get_labels.return_value = ["ready-for-agent"]  # AFK
    mock_github.get_issue_title.return_value = "Fix preflight"

    def _run_host_checks(_):
        check_call_count[0] += 1
        if check_call_count[0] == 1:
            return [("pytest", "pytest", "FAILED")]  # post-merge check fails
        return []  # second check (after AFK fix) passes

    async def _fake_run_agent(name, skip_preflight=False, **kwargs):
        if name == "Planner":
            planner_skip_flags.append(skip_preflight)
            planner_call_count[0] += 1
            if planner_call_count[0] == 1:
                return _plan_json([{"number": 1, "title": "Fix"}])
            return _plan_json([])  # second iteration: no issues → terminate
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        if "preflight-issue" in name:
            return "<issue>99</issue>"
        return ""

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=mock_git,
        github_service=mock_github,
        run_host_checks=_run_host_checks,
        max_iterations=2,
    )

    assert len(planner_skip_flags) == 2, (
        f"Expected 2 Planner calls; got {len(planner_skip_flags)}"
    )
    assert planner_skip_flags[1] is True, (
        "Next Planner must skip preflight when AFK post-merge fix passed second check"
    )


# ── Issue-187: implementer and reviewer skip preflight ───────────────────────


def test_implementer_invoked_with_skip_preflight_true(tmp_path):
    """run_issue must pass skip_preflight=True to the implementer agent."""
    captured: list[dict] = []

    async def _fake_run_agent(name, skip_preflight=False, **kwargs):
        captured.append({"name": name, "skip_preflight": skip_preflight})
        return "<promise>COMPLETE</promise>"

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
        return "<promise>COMPLETE</promise>"

    issue = {"number": 1, "title": "Fix thing"}
    asyncio.run(run_issue(issue, {}, tmp_path, run_agent=_fake_run_agent))

    rev_call = next(c for c in captured if "Reviewer" in c["name"])
    assert rev_call["skip_preflight"] is True, (
        f"Reviewer must receive skip_preflight=True; got {rev_call['skip_preflight']!r}"
    )


def test_planner_skip_preflight_controlled_by_caller(tmp_path):
    """Planner skip_preflight must be controlled by the orchestrator, not hardcoded True."""
    captured: list[dict] = []

    async def _fake_run_agent(name, skip_preflight=False, **kwargs):
        captured.append({"name": name, "skip_preflight": skip_preflight})
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 1, "title": "Fix"}])

    mock_git = _make_git_svc(try_merge_side_effect=[True, True])
    mock_git.get_head_sha.return_value = "any_sha"

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=mock_git,
        github_service=_make_github_svc(),
        run_host_checks=lambda _: [],
        max_iterations=2,
    )

    planner_calls = [c for c in captured if c["name"] == "Planner"]
    assert planner_calls[0]["skip_preflight"] is False, (
        "Planner on cold startup must not skip preflight"
    )
    assert planner_calls[1]["skip_preflight"] is True, (
        "Planner on second iteration (after clean post-merge) must skip preflight"
    )


def test_post_merge_preflight_fix_calls_close_issue_and_close_completed_parent_issues(
    tmp_path,
):
    """When an AFK post-merge preflight-fix issue merges, close_issue() must be called
    for the fix issue and close_completed_parent_issues() must be called."""
    check_call_count = [0]
    mock_git = _make_git_svc(try_merge_side_effect=[True, True])
    mock_git.get_head_sha.return_value = "sha"

    mock_github = MagicMock(spec=GithubService)
    mock_github.get_labels.return_value = ["ready-for-agent"]  # AFK
    mock_github.get_issue_title.return_value = "Fix preflight"

    def _run_host_checks(_):
        check_call_count[0] += 1
        if check_call_count[0] == 1:
            return [("pytest", "pytest", "FAILED")]  # post-merge check fails
        return []  # second check passes after fix

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json([{"number": 1, "title": "Fix"}])
        if "preflight-issue" in name:
            return "<issue>55</issue>"
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return ""

    _run(
        tmp_path,
        _fake_run_agent,
        git_service=mock_git,
        github_service=mock_github,
        run_host_checks=_run_host_checks,
    )

    closed = [call.args[0] for call in mock_github.close_issue.call_args_list]
    assert 55 in closed, (
        f"Preflight fix issue #55 must be closed via close_issue; got {closed}"
    )
    assert mock_github.close_completed_parent_issues.call_count >= 2, (
        "close_completed_parent_issues must be called after main merge and after preflight-fix merge"
    )


def test_postmerge_preflight_issue_fixer_invoked_with_skip_preflight_true(tmp_path):
    """preflight-issue agent spawned for post-merge failures must pass skip_preflight=True (no regression)."""
    captured: list[dict] = []

    async def _fake_run_agent(name, skip_preflight=False, **kwargs):
        captured.append({"name": name, "skip_preflight": skip_preflight})
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        if "preflight-issue" in name:
            return "<issue>55</issue>"
        return _plan_json([{"number": 1, "title": "Fix"}])

    failures = [("pytest", "pytest", "FAILED")]
    with pytest.raises(SystemExit):
        _run(
            tmp_path,
            _fake_run_agent,
            git_service=_make_git_svc(try_merge_side_effect=[True]),
            github_service=_make_github_svc_hitl(),
            run_host_checks=lambda _: failures,
        )

    pf_calls = [c for c in captured if "preflight-issue" in c["name"]]
    assert pf_calls, "preflight-issue agent must be spawned"
    assert pf_calls[0]["skip_preflight"] is True, (
        f"preflight-issue agent must receive skip_preflight=True; got {pf_calls[0]['skip_preflight']!r}"
    )


# ── Issue-183: orchestrator exit handling for usage-limit shutdown ─────────────


def test_usage_limit_error_exits_with_code_1(tmp_path):
    """When UsageLimitError is raised by an agent task, orchestrator must exit with code 1."""
    from pycastle.errors import UsageLimitError

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json([{"number": 1, "title": "Fix"}])
        raise UsageLimitError("You've hit your session limit")

    with pytest.raises(SystemExit) as exc_info:
        _run(tmp_path, _fake_run_agent, github_service=_make_github_svc())

    assert exc_info.value.code == 1


def test_usage_limit_error_prints_resume_message_to_stderr(tmp_path, capsys):
    """When UsageLimitError is raised by an agent task, the resume message must be printed to stderr."""
    from pycastle.errors import UsageLimitError

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json([{"number": 1, "title": "Fix"}])
        raise UsageLimitError("You've hit your session limit")

    with pytest.raises(SystemExit):
        _run(tmp_path, _fake_run_agent, github_service=_make_github_svc())

    err = capsys.readouterr().err
    assert (
        "Usage limit reached. Worktrees preserved. Run 'pycastle run' again to resume."
        in err
    )


def test_usage_limit_error_awaits_sibling_tasks(tmp_path):
    """When one agent raises UsageLimitError, sibling tasks must run to completion before exit."""
    from pycastle.errors import UsageLimitError

    completed_agents: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json(
                [{"number": 1, "title": "Fail"}, {"number": 2, "title": "Pass"}]
            )
        if "Implementer #1" in name:
            raise UsageLimitError("You've hit your session limit")
        completed_agents.append(name)
        return "<promise>COMPLETE</promise>"

    with pytest.raises(SystemExit):
        _run(
            tmp_path, _fake_run_agent, github_service=_make_github_svc(), max_parallel=4
        )

    assert any("Implementer #2" in n for n in completed_agents), (
        f"Sibling Implementer #2 must complete before exit; completed={completed_agents}"
    )


def test_usage_limit_error_not_written_to_errors_log(tmp_path):
    """UsageLimitError must not be logged to errors.log (unlike regular exceptions)."""
    from pycastle.errors import UsageLimitError

    logs_dir = tmp_path / "pycastle" / "logs"
    logs_dir.mkdir(parents=True)
    errors_log = logs_dir / "errors.log"

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json([{"number": 1, "title": "Fix"}])
        raise UsageLimitError("You've hit your session limit")

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
    from pycastle.errors import UsageLimitError

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json(
                [{"number": 1, "title": "Limit"}, {"number": 2, "title": "Other"}]
            )
        if "Implementer #1" in name:
            raise UsageLimitError("session limit")
        if "Implementer #2" in name:
            raise RuntimeError("unrelated failure")

    with pytest.raises(SystemExit) as exc_info:
        _run(
            tmp_path, _fake_run_agent, github_service=_make_github_svc(), max_parallel=4
        )

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert (
        "Usage limit reached. Worktrees preserved. Run 'pycastle run' again to resume."
        in err
    )


def test_usage_limit_error_in_post_merge_preflight_exits_with_code_1(tmp_path, capsys):
    """UsageLimitError raised in the post-merge preflight run_issue must exit with code 1."""
    from pycastle.errors import UsageLimitError

    mock_git = _make_git_svc(try_merge_side_effect=[True])
    mock_git.get_head_sha.return_value = "sha"
    mock_github = MagicMock(spec=GithubService)
    mock_github.get_labels.return_value = ["ready-for-agent"]  # AFK
    mock_github.get_issue_title.return_value = "Fix preflight"

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json([{"number": 1, "title": "Fix"}])
        if "preflight-issue" in name:
            return "<issue>55</issue>"
        if name == "Implementer #1":
            return "<promise>COMPLETE</promise>"
        if name == "Reviewer #1":
            return ""
        raise UsageLimitError("session limit during preflight fix")

    with pytest.raises(SystemExit) as exc_info:
        _run(
            tmp_path,
            _fake_run_agent,
            git_service=mock_git,
            github_service=mock_github,
            run_host_checks=lambda _: [("pytest", "pytest", "FAILED")],
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
        return ""

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
        return ""

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
        return ""

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
