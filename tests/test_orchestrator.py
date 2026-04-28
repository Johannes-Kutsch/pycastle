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
    delete_merged_branches,
    parse_plan,
    prune_orphan_worktrees,
    run,
    run_issue,
    wait_for_clean_working_tree,
)


# ── parse_plan ────────────────────────────────────────────────────────────────


def test_parse_plan_returns_issues_list():
    output = '<plan>{"issues": [{"number": 1, "title": "Fix bug", "branch": "issue/1"}]}</plan>'
    assert parse_plan(output) == [
        {"number": 1, "title": "Fix bug", "branch": "issue/1"}
    ]


def test_parse_plan_returns_empty_list_when_no_issues():
    output = '<plan>{"issues": []}</plan>'
    assert parse_plan(output) == []


def test_parse_plan_raises_when_no_plan_tag():
    with pytest.raises(RuntimeError, match="no <plan> tag"):
        parse_plan("some output with no plan tag")


def test_parse_plan_returns_unblocked_issues_list():
    output = '<plan>{"unblocked_issues": [{"number": 2, "title": "Do thing", "branch": "issue/2"}], "blocked_issues": [{"number": 3, "title": "Later"}]}</plan>'
    assert parse_plan(output) == [
        {"number": 2, "title": "Do thing", "branch": "issue/2"}
    ]


def test_parse_plan_raises_descriptively_when_issues_key_missing():
    output = '<plan>{"something_else": []}</plan>'
    with pytest.raises(
        RuntimeError, match="'unblocked_issues'.*'issues'|'issues'.*'unblocked_issues'"
    ):
        parse_plan(output)


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
    return MagicMock(spec=GithubService)


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
            return _plan_json(
                [{"number": 1, "title": "Fix thing", "branch": "issue/1"}]
            )
        raise boom

    _run(tmp_path, _fake_run_agent, logs_dir=logs_dir)

    content = errors_log.read_text()
    assert "RuntimeError" in content
    assert "something went wrong" in content


def test_failed_agent_errors_log_has_timestamp_separator(tmp_path):
    logs_dir = tmp_path / "pycastle" / "logs"
    logs_dir.mkdir(parents=True)
    errors_log = logs_dir / "errors.log"

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json(
                [{"number": 1, "title": "Fix thing", "branch": "issue/1"}]
            )
        raise RuntimeError("boom")

    _run(tmp_path, _fake_run_agent, logs_dir=logs_dir)

    assert "---" in errors_log.read_text()


def test_failed_agent_prints_traceback_to_stderr(tmp_path, capsys):
    logs_dir = tmp_path / "pycastle" / "logs"
    logs_dir.mkdir(parents=True)

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json(
                [{"number": 1, "title": "Fix thing", "branch": "issue/1"}]
            )
        raise RuntimeError("stderr traceback check")

    _run(tmp_path, _fake_run_agent, logs_dir=logs_dir)

    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert "stderr traceback check" in err


# ── Cycle 50-4: FEEDBACK_COMMANDS passed to implementer ──────────────────────


def test_run_issue_passes_feedback_commands_to_implementer(tmp_path):
    """run_issue must include FEEDBACK_COMMANDS in prompt_args for the implementer."""
    captured_args: list[dict] = []

    async def _fake_run_agent(
        name, prompt_file, mount_path, env, prompt_args=None, **kw
    ):
        captured_args.append({"name": name, "prompt_args": prompt_args or {}})
        return "<promise>COMPLETE</promise>"

    issue = {"number": 1, "title": "Fix thing", "branch": "issue/1"}
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

    issue = {"number": 1, "title": "Fix thing", "branch": "issue/1"}
    asyncio.run(run_issue(issue, {}, tmp_path, run_agent=_fake_run_agent))

    implementer_call = next(a for a in captured_args if "Implementer" in a["name"])
    feedback_commands = implementer_call["prompt_args"]["FEEDBACK_COMMANDS"]
    for cmd in IMPLEMENT_CHECKS:
        assert f"`{cmd}`" in feedback_commands


# ── Cycle 52-1: planner PreflightError → no implementers spawned ─────────────


def test_planner_preflight_error_spawns_no_implementers(tmp_path):
    """A PreflightError from the planner must abort the run with no implementer agents spawned."""
    implementer_names: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            raise PreflightError([("ruff", "ruff check .", "E501 line too long")])
        implementer_names.append(name)
        return ""

    _run(tmp_path, _fake_run_agent)

    assert implementer_names == [], (
        f"Expected no implementers, got: {implementer_names}"
    )


def test_planner_preflight_error_run_exits_cleanly(tmp_path):
    """A PreflightError from the planner must not propagate out of run()."""

    async def _fake_run_agent(name, **kwargs):
        raise PreflightError([("ruff", "ruff check .", "E501")])

    _run(tmp_path, _fake_run_agent)  # must not raise


def test_planner_preflight_error_message_names_failed_checks(tmp_path, capsys):
    """Aborting due to planner PreflightError must print the check name and command."""

    async def _fake_run_agent(name, **kwargs):
        raise PreflightError([("ruff", "ruff check .", "E501 line too long")])

    _run(tmp_path, _fake_run_agent)

    out = capsys.readouterr().out
    assert "ruff" in out
    assert "ruff check ." in out


# ── Cycle 52-2: implementer PreflightError → siblings complete ───────────────


def test_implementer_preflight_error_siblings_complete(tmp_path):
    """An implementer PreflightError must not prevent sibling issues from completing."""
    completed_issues: list[int] = []

    issues = [
        {"number": 1, "title": "Issue one", "branch": "issue/1"},
        {"number": 2, "title": "Issue two", "branch": "issue/2"},
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
            return _plan_json(
                [{"number": 3, "title": "Fix types", "branch": "issue/3"}]
            )
        raise PreflightError([("mypy", "mypy .", "error: Cannot find module")])

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    _run(tmp_path, _fake_run_agent, logs_dir=logs_dir)

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

    _run(tmp_path, _fake_run_agent, validate_config=_fake_validate)

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

    _run(tmp_path, _fake_run_agent, stage_overrides=stage_overrides)

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
        return _plan_json([{"number": 1, "title": "Fix", "branch": "issue/1"}])

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
        return _plan_json([{"number": 1, "title": "Fix", "branch": "issue/1"}])

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
        return _plan_json([{"number": 1, "title": "Fix", "branch": "issue/1"}])

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

    _run(tmp_path, _fake_run_agent, stage_overrides=stage_overrides)

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
        return _plan_json([{"number": 1, "title": "Fix", "branch": "issue/1"}])

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
        return _plan_json([{"number": 1, "title": "Fix", "branch": "issue/1"}])

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
        return _plan_json([{"number": 1, "title": "Fix", "branch": "issue/1"}])

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
        {"number": 1, "title": "Fix A", "branch": "issue/1"},
        {"number": 2, "title": "Fix B", "branch": "issue/2"},
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


def test_clean_merge_calls_close_issue_with_parents(tmp_path):
    """Each cleanly-merged issue must be closed via close_issue_with_parents."""
    issues = [
        {"number": 7, "title": "Fix A", "branch": "issue/7"},
        {"number": 8, "title": "Fix B", "branch": "issue/8"},
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

    closed = [
        call.args[0] for call in mock_github.close_issue_with_parents.call_args_list
    ]
    assert sorted(closed) == [7, 8], f"Expected issues 7 and 8 closed; got {closed}"


def test_conflict_branch_spawns_merger_with_only_failing_branch(tmp_path):
    """When one branch conflicts, Merger is spawned with only the conflicting branch."""
    captured: list[dict] = []

    issues = [
        {"number": 1, "title": "Clean", "branch": "issue/1"},
        {"number": 2, "title": "Conflict", "branch": "issue/2"},
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
    assert "issue/2" in branches_arg
    assert "issue/1" not in branches_arg


def test_conflict_branch_skips_post_merge_checks(tmp_path):
    """When any branch conflicts, post-merge host checks must not run."""
    host_checks_called = []

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 1, "title": "Conflict", "branch": "issue/1"}])

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
        return _plan_json([{"number": 1, "title": "Fix", "branch": "issue/1"}])

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


def test_post_merge_check_failure_spawns_bug_report_not_merger(tmp_path):
    """On post-merge check failure, bug-report is spawned and Merger is NOT spawned."""
    agent_names: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        agent_names.append(name)
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 1, "title": "Fix", "branch": "issue/1"}])

    failures = [("pytest", "pytest", "FAILED tests/test_foo.py")]
    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        github_service=_make_github_svc(),
        run_host_checks=lambda _: failures,
    )

    assert "Merger" not in agent_names, (
        f"Merger must not spawn on check failure; agents={agent_names}"
    )
    bug_report_names = [n for n in agent_names if "bug-report" in n]
    assert len(bug_report_names) >= 1, (
        f"At least one bug-report agent expected; agents={agent_names}"
    )


def test_post_merge_bug_report_uses_post_merge_stage(tmp_path):
    """Bug-report agents spawned on post-merge failure must use '[post-merge]' in CHECK_NAME."""
    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append({"name": name, "prompt_args": kwargs.get("prompt_args", {})})
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 1, "title": "Fix", "branch": "issue/1"}])

    failures = [("pytest", "pytest", "FAILED")]
    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        github_service=_make_github_svc(),
        run_host_checks=lambda _: failures,
    )

    bug_calls = [c for c in captured if "bug-report" in c["name"]]
    assert bug_calls, "Expected bug-report agent calls"
    check_name = bug_calls[0]["prompt_args"].get("CHECK_NAME", "")
    assert "[post-merge]" in check_name, (
        f"CHECK_NAME must include '[post-merge]'; got {check_name!r}"
    )


def test_conflict_branch_does_not_close_issue(tmp_path):
    """Conflicting branches must not be closed via close_issue_with_parents."""
    issues = [
        {"number": 1, "title": "Clean", "branch": "issue/1"},
        {"number": 2, "title": "Conflict", "branch": "issue/2"},
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

    closed = [
        call.args[0] for call in mock_github.close_issue_with_parents.call_args_list
    ]
    assert 2 not in closed, f"Conflict issue #2 must not be closed; closed: {closed}"
    assert 1 in closed, f"Clean issue #1 must be closed; closed: {closed}"


def test_merger_receives_correct_issues_prompt_arg(tmp_path):
    """Merger ISSUES prompt arg must list only the conflicting issues, not clean ones."""
    captured: list[dict] = []

    issues = [
        {"number": 3, "title": "Clean issue", "branch": "issue/3"},
        {"number": 4, "title": "Conflict issue", "branch": "issue/4"},
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
    issues_arg = merger_calls[0]["prompt_args"]["ISSUES"]
    assert "#4" in issues_arg, (
        f"Conflict issue #4 must appear in ISSUES; got {issues_arg!r}"
    )
    assert "#3" not in issues_arg, (
        f"Clean issue #3 must not appear in ISSUES; got {issues_arg!r}"
    )


def test_multiple_check_failures_spawn_one_bug_report_each(tmp_path):
    """Each post-merge check failure must spawn exactly one bug-report agent."""
    agent_names: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        agent_names.append(name)
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 1, "title": "Fix", "branch": "issue/1"}])

    failures = [
        ("pytest", "pytest", "FAILED tests/test_foo.py"),
        ("mypy", "mypy .", "error: Found 2 errors"),
        ("ruff", "ruff check .", "ruff: error"),
    ]
    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        github_service=_make_github_svc(),
        run_host_checks=lambda _: failures,
    )

    bug_reports = [n for n in agent_names if "bug-report" in n]
    assert len(bug_reports) == 3, (
        f"Expected 3 bug-report agents (one per failure); got {bug_reports}"
    )


def test_bug_report_receives_correct_command_and_output(tmp_path):
    """Bug-report prompt_args must include the exact COMMAND and OUTPUT from the failing check."""
    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append({"name": name, "prompt_args": kwargs.get("prompt_args", {})})
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 1, "title": "Fix", "branch": "issue/1"}])

    failures = [("pytest", "pytest -x", "FAILED tests/test_bar.py::test_something")]
    _run(
        tmp_path,
        _fake_run_agent,
        git_service=_make_git_svc(try_merge_side_effect=[True]),
        github_service=_make_github_svc(),
        run_host_checks=lambda _: failures,
    )

    bug_calls = [c for c in captured if "bug-report" in c["name"]]
    assert len(bug_calls) == 1
    args = bug_calls[0]["prompt_args"]
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
        return _plan_json([{"number": 1, "title": "Fix A", "branch": "issue/1"}])

    mock_git = _make_git_svc(try_merge_side_effect=[True], is_ancestor=True)
    _run(
        tmp_path,
        _fake_run_agent,
        git_service=mock_git,
        github_service=_make_github_svc(),
    )

    mock_git.delete_branch.assert_called_with("issue/1", tmp_path)


def test_conflict_branches_are_deleted_after_merger_agent(tmp_path):
    """Branches resolved by the Merger agent must be deleted after it returns."""

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 2, "title": "Conflict", "branch": "issue/2"}])

    mock_git = _make_git_svc(try_merge_side_effect=[False], is_ancestor=True)
    _run(
        tmp_path,
        _fake_run_agent,
        git_service=mock_git,
        github_service=_make_github_svc(),
    )

    mock_git.delete_branch.assert_called_with("issue/2", tmp_path)


def test_non_ancestor_branch_not_deleted(tmp_path):
    """A branch that is not an ancestor of HEAD must not be deleted."""

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return _plan_json([{"number": 1, "title": "Fix A", "branch": "issue/1"}])

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
        return _plan_json([{"number": 1, "title": "Fix A", "branch": "issue/1"}])

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

    issue = {"number": 1, "title": "Fix thing", "branch": "issue/1"}
    result = asyncio.run(run_issue(issue, {}, tmp_path, run_agent=_fake_run_agent))

    assert result is None


def test_run_issue_returns_issue_when_implementer_completes(tmp_path):
    """run_issue must return the issue dict when implementer produces COMPLETE."""

    async def _fake_run_agent(**kwargs):
        return "<promise>COMPLETE</promise>"

    issue = {"number": 2, "title": "Fix thing", "branch": "issue/2"}
    result = asyncio.run(run_issue(issue, {}, tmp_path, run_agent=_fake_run_agent))

    assert result == issue


def test_run_incomplete_implementers_skip_merge(tmp_path):
    """When no implementer produces COMPLETE, try_merge must never be called."""

    async def _fake_run_agent(name, **kwargs):
        if name == "Planner":
            return _plan_json([{"number": 1, "title": "Fix", "branch": "issue/1"}])
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
            return _plan_json([{"number": 1, "title": "Fix", "branch": "issue/1"}])
        raise RuntimeError("agent failed")

    _run(tmp_path, _fake_run_agent, logs_dir=logs_dir)

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
        return _plan_json([{"number": 1, "title": "Fix", "branch": "issue/1"}])

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