import contextlib
from pathlib import Path
from unittest.mock import MagicMock, patch

from pycastle.git_service import GitService
from pycastle.orchestrator import prune_orphan_worktrees


@contextlib.contextmanager
def _merge_patches(*, try_merge_return: bool = True):
    """Context manager that satisfies orchestrator's merge-loop dependencies."""
    mock_git = MagicMock(spec=GitService)
    mock_git.try_merge.return_value = try_merge_return
    with (
        patch("pycastle.orchestrator.GitService", return_value=mock_git),
        patch("pycastle.orchestrator.GithubService", return_value=MagicMock()),
        patch("pycastle.orchestrator._get_repo", return_value="owner/repo"),
        patch("pycastle.orchestrator._run_host_checks", return_value=[]),
    ):
        yield mock_git


# ── Cycle 24-B1: prune_orphan_worktrees deletes orphan dirs ──────────────────


def _make_git_service(active_paths: list[Path]) -> GitService:
    mock_svc = MagicMock(spec=GitService)
    mock_svc.list_worktrees.return_value = active_paths
    return mock_svc


def test_prune_orphan_worktrees_deletes_absent_dir(tmp_path):
    worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)
    orphan = worktrees_dir / "orphan-branch"
    orphan.mkdir()

    prune_orphan_worktrees(tmp_path, git_service=_make_git_service([]))

    assert not orphan.exists()


def test_prune_orphan_worktrees_deletes_only_orphans(tmp_path):
    """Only dirs absent from git worktree list must be deleted; active ones survive."""
    worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)
    orphan = worktrees_dir / "orphan"
    orphan.mkdir()
    active = worktrees_dir / "active-branch"
    active.mkdir()

    prune_orphan_worktrees(tmp_path, git_service=_make_git_service([active]))

    assert not orphan.exists()
    assert active.exists()


# ── Cycle 24-B2: prune_orphan_worktrees preserves active worktrees ───────────


def test_prune_orphan_worktrees_preserves_active_dir(tmp_path):
    worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)
    active = worktrees_dir / "my-branch"
    active.mkdir()

    prune_orphan_worktrees(tmp_path, git_service=_make_git_service([active]))

    assert active.exists()


def test_prune_orphan_worktrees_noop_when_dir_missing(tmp_path):
    """Must not raise if pycastle/.worktrees/ does not exist yet."""
    prune_orphan_worktrees(tmp_path)  # no exception — no git_service needed


def test_prune_orphan_worktrees_calls_list_worktrees_with_repo_root(tmp_path):
    worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)
    mock_svc = _make_git_service([])
    prune_orphan_worktrees(tmp_path, git_service=mock_svc)
    mock_svc.list_worktrees.assert_called_once_with(tmp_path)


# ── Cycle 24-B3: run() calls prune_orphan_worktrees before the loop ──────────


def test_run_calls_prune_before_iteration_loop(tmp_path):
    call_order: list[str] = []

    def _fake_prune(repo_root):
        call_order.append("prune")

    async def _fake_run_agent(*args, **kwargs):
        call_order.append("agent")
        return "<plan>[]</plan>"

    import asyncio
    from pycastle.orchestrator import run

    with (
        patch("pycastle.orchestrator.prune_orphan_worktrees", side_effect=_fake_prune),
        patch("pycastle.orchestrator.run_agent", side_effect=_fake_run_agent),
        patch("pycastle.orchestrator.parse_plan", return_value=[]),
    ):
        asyncio.run(run({}, tmp_path))

    assert call_order[0] == "prune", f"prune must be first call, got: {call_order}"


# ── Cycle 24-C1/C2: error logging on agent failure ───────────────────────────


def _make_failing_run(tmp_path, exc: Exception):
    """Return a coroutine factory that drives run() with one issue that raises exc."""
    import asyncio
    from pycastle.orchestrator import run

    async def _fake_run_agent(
        name, prompt_file, mount_path, env, prompt_args=None, **kw
    ):
        if name == "Planner":
            return "<plan>placeholder</plan>"
        raise exc

    def _go():
        with (
            patch("pycastle.orchestrator.prune_orphan_worktrees"),
            patch("pycastle.orchestrator.run_agent", side_effect=_fake_run_agent),
            patch(
                "pycastle.orchestrator.parse_plan",
                return_value=[{"number": 1, "title": "Fix thing", "branch": "issue/1"}],
            ),
        ):
            asyncio.run(run({}, tmp_path))

    return _go


def test_failed_agent_appends_traceback_to_errors_log(tmp_path, capsys):
    logs_dir = tmp_path / "pycastle" / "logs"
    logs_dir.mkdir(parents=True)
    errors_log = logs_dir / "errors.log"

    boom = RuntimeError("something went wrong")
    run_it = _make_failing_run(tmp_path, boom)

    with patch("pycastle.orchestrator.LOGS_DIR", logs_dir):
        run_it()

    content = errors_log.read_text()
    assert "RuntimeError" in content
    assert "something went wrong" in content


def test_failed_agent_errors_log_has_timestamp_separator(tmp_path, capsys):
    logs_dir = tmp_path / "pycastle" / "logs"
    logs_dir.mkdir(parents=True)
    errors_log = logs_dir / "errors.log"

    run_it = _make_failing_run(tmp_path, RuntimeError("boom"))

    with patch("pycastle.orchestrator.LOGS_DIR", logs_dir):
        run_it()

    assert "---" in errors_log.read_text()


def test_failed_agent_prints_traceback_to_stderr(tmp_path, capsys):
    logs_dir = tmp_path / "pycastle" / "logs"
    logs_dir.mkdir(parents=True)

    boom = RuntimeError("stderr traceback check")
    run_it = _make_failing_run(tmp_path, boom)

    with patch("pycastle.orchestrator.LOGS_DIR", logs_dir):
        run_it()

    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert "stderr traceback check" in err


# ── Cycle 50-4: FEEDBACK_COMMANDS passed to implementer ──────────────────────


def test_run_issue_passes_feedback_commands_to_implementer(tmp_path):
    """run_issue must include FEEDBACK_COMMANDS in prompt_args for the implementer."""
    import asyncio
    from pycastle.orchestrator import run_issue

    captured_args: list[dict] = []

    async def _fake_run_agent(
        name, prompt_file, mount_path, env, prompt_args=None, **kw
    ):
        captured_args.append({"name": name, "prompt_args": prompt_args or {}})
        return "<promise>COMPLETE</promise>"

    issue = {"number": 1, "title": "Fix thing", "branch": "issue/1"}
    with patch("pycastle.orchestrator.run_agent", side_effect=_fake_run_agent):
        asyncio.run(run_issue(issue, {}, tmp_path))

    implementer_call = next(a for a in captured_args if "Implementer" in a["name"])
    assert "FEEDBACK_COMMANDS" in implementer_call["prompt_args"]


def test_run_issue_feedback_commands_formatted_from_implement_checks(tmp_path):
    """FEEDBACK_COMMANDS must be formatted from IMPLEMENT_CHECKS with backtick wrapping."""
    import asyncio
    from pycastle.defaults.config import IMPLEMENT_CHECKS
    from pycastle.orchestrator import run_issue

    captured_args: list[dict] = []

    async def _fake_run_agent(
        name, prompt_file, mount_path, env, prompt_args=None, **kw
    ):
        captured_args.append({"name": name, "prompt_args": prompt_args or {}})
        return "<promise>COMPLETE</promise>"

    issue = {"number": 1, "title": "Fix thing", "branch": "issue/1"}
    with patch("pycastle.orchestrator.run_agent", side_effect=_fake_run_agent):
        asyncio.run(run_issue(issue, {}, tmp_path))

    implementer_call = next(a for a in captured_args if "Implementer" in a["name"])
    feedback_commands = implementer_call["prompt_args"]["FEEDBACK_COMMANDS"]
    for cmd in IMPLEMENT_CHECKS:
        assert f"`{cmd}`" in feedback_commands


# ── Cycle 52-1: planner PreflightError → no implementers spawned ─────────────


def test_planner_preflight_error_spawns_no_implementers(tmp_path):
    """A PreflightError from the planner must abort the run with no implementer agents spawned."""
    import asyncio
    from pycastle.errors import PreflightError
    from pycastle.orchestrator import run

    implementer_names: list[str] = []

    async def _fake_run_agent(
        name, prompt_file, mount_path, env, prompt_args=None, **kw
    ):
        if name == "Planner":
            raise PreflightError([("ruff", "ruff check .", "E501 line too long")])
        implementer_names.append(name)
        return ""

    with (
        patch("pycastle.orchestrator.prune_orphan_worktrees"),
        patch("pycastle.orchestrator.run_agent", side_effect=_fake_run_agent),
    ):
        asyncio.run(run({}, tmp_path))

    assert implementer_names == [], (
        f"Expected no implementers, got: {implementer_names}"
    )


def test_planner_preflight_error_run_exits_cleanly(tmp_path):
    """A PreflightError from the planner must not propagate out of run()."""
    import asyncio
    from pycastle.errors import PreflightError
    from pycastle.orchestrator import run

    async def _fake_run_agent(
        name, prompt_file, mount_path, env, prompt_args=None, **kw
    ):
        raise PreflightError([("ruff", "ruff check .", "E501")])

    with (
        patch("pycastle.orchestrator.prune_orphan_worktrees"),
        patch("pycastle.orchestrator.run_agent", side_effect=_fake_run_agent),
    ):
        asyncio.run(run({}, tmp_path))  # must not raise


def test_planner_preflight_error_message_names_failed_checks(tmp_path, capsys):
    """Aborting due to planner PreflightError must print the check name and command."""
    import asyncio
    from pycastle.errors import PreflightError
    from pycastle.orchestrator import run

    async def _fake_run_agent(
        name, prompt_file, mount_path, env, prompt_args=None, **kw
    ):
        raise PreflightError([("ruff", "ruff check .", "E501 line too long")])

    with (
        patch("pycastle.orchestrator.prune_orphan_worktrees"),
        patch("pycastle.orchestrator.run_agent", side_effect=_fake_run_agent),
    ):
        asyncio.run(run({}, tmp_path))

    out = capsys.readouterr().out
    assert "ruff" in out
    assert "ruff check ." in out


# ── Cycle 52-2: implementer PreflightError → siblings complete ───────────────


def test_implementer_preflight_error_siblings_complete(tmp_path):
    """An implementer PreflightError must not prevent sibling issues from completing."""
    import asyncio
    from pycastle.errors import PreflightError
    from pycastle.orchestrator import run

    completed_issues: list[int] = []

    async def _fake_run_agent(
        name, prompt_file, mount_path, env, prompt_args=None, **kw
    ):
        if name == "Planner":
            return "<plan>placeholder</plan>"
        if name == "Implementer #1":
            raise PreflightError([("ruff", "ruff check .", "E501")])
        if "Implementer" in name:
            completed_issues.append(int(name.split("#")[1]))
            return "<promise>COMPLETE</promise>"
        return ""

    with (
        patch("pycastle.orchestrator.prune_orphan_worktrees"),
        patch("pycastle.orchestrator.run_agent", side_effect=_fake_run_agent),
        patch(
            "pycastle.orchestrator.parse_plan",
            return_value=[
                {"number": 1, "title": "Issue one", "branch": "issue/1"},
                {"number": 2, "title": "Issue two", "branch": "issue/2"},
            ],
        ),
    ):
        with _merge_patches():
            asyncio.run(run({}, tmp_path))

    assert 2 in completed_issues, (
        f"Issue #2 must complete; completed: {completed_issues}"
    )


def test_implementer_preflight_error_logs_check_details(tmp_path, capsys):
    """An implementer PreflightError must print the failed check name and command to stdout."""
    import asyncio
    from pycastle.errors import PreflightError
    from pycastle.orchestrator import run

    async def _fake_run_agent(
        name, prompt_file, mount_path, env, prompt_args=None, **kw
    ):
        if name == "Planner":
            return "<plan>placeholder</plan>"
        raise PreflightError([("mypy", "mypy .", "error: Cannot find module")])

    with (
        patch("pycastle.orchestrator.prune_orphan_worktrees"),
        patch("pycastle.orchestrator.run_agent", side_effect=_fake_run_agent),
        patch(
            "pycastle.orchestrator.parse_plan",
            return_value=[{"number": 3, "title": "Fix types", "branch": "issue/3"}],
        ),
        patch("pycastle.orchestrator.LOGS_DIR", tmp_path),
    ):
        asyncio.run(run({}, tmp_path))

    out = capsys.readouterr().out
    # Must show per-check formatted line, not just the raw exception repr
    assert "mypy" in out
    assert "mypy ." in out
    assert "[('mypy'" not in out, (
        "Output must not be raw tuple repr — format each check explicitly"
    )


# ── Issue-78: validate_config called at start of run() ───────────────────────


def test_run_calls_validate_config_before_any_agent(tmp_path):
    """validate_config must be called before the first run_agent call."""
    import asyncio
    from pycastle.orchestrator import run

    call_order: list[str] = []

    def _fake_validate(overrides):
        call_order.append("validate")

    async def _fake_run_agent(*args, **kwargs):
        call_order.append("agent")
        return "<plan>[]</plan>"

    with (
        patch("pycastle.orchestrator.prune_orphan_worktrees"),
        patch("pycastle.orchestrator.validate_config", side_effect=_fake_validate),
        patch("pycastle.orchestrator.run_agent", side_effect=_fake_run_agent),
        patch("pycastle.orchestrator.parse_plan", return_value=[]),
    ):
        asyncio.run(run({}, tmp_path))

    assert call_order[0] == "validate", f"validate must be first; got {call_order}"


def test_run_validate_config_error_propagates_no_agents_started(tmp_path):
    """ConfigValidationError from validate_config must propagate and prevent all agents."""
    import asyncio
    import pytest
    from pycastle.errors import ConfigValidationError
    from pycastle.orchestrator import run

    agents_started: list[str] = []

    async def _fake_run_agent(*args, **kwargs):
        agents_started.append(kwargs.get("name", args[0] if args else "?"))
        return ""

    with (
        patch("pycastle.orchestrator.prune_orphan_worktrees"),
        patch(
            "pycastle.orchestrator.validate_config",
            side_effect=ConfigValidationError("bad model"),
        ),
        patch("pycastle.orchestrator.run_agent", side_effect=_fake_run_agent),
    ):
        with pytest.raises(ConfigValidationError):
            asyncio.run(run({}, tmp_path))

    assert agents_started == [], f"No agents must start; got {agents_started}"


# ── Issue-78: _stage_for_agent helper ─────────────────────────────────────────


def test_stage_for_agent_planner():
    from pycastle.orchestrator import _stage_for_agent

    assert _stage_for_agent("Planner") == "plan"


def test_stage_for_agent_implementer():
    from pycastle.orchestrator import _stage_for_agent

    assert _stage_for_agent("Implementer #42") == "implement"


def test_stage_for_agent_reviewer():
    from pycastle.orchestrator import _stage_for_agent

    assert _stage_for_agent("Reviewer #7") == "review"


def test_stage_for_agent_merger():
    from pycastle.orchestrator import _stage_for_agent

    assert _stage_for_agent("Merger") == "merge"


# ── Issue-78: model/effort passed per stage ───────────────────────────────────


def test_planner_receives_plan_stage_model_and_effort(tmp_path):
    """Planner run_agent call must include model and effort from plan stage override."""
    import asyncio
    from pycastle.orchestrator import run

    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append(
            {"name": name, "model": kwargs.get("model"), "effort": kwargs.get("effort")}
        )
        return "<plan>[]</plan>"

    stage_overrides = {
        "plan": {"model": "claude-haiku-4-5", "effort": "low"},
        "implement": {"model": "", "effort": ""},
        "review": {"model": "", "effort": ""},
        "merge": {"model": "", "effort": ""},
    }

    with (
        patch("pycastle.orchestrator.prune_orphan_worktrees"),
        patch("pycastle.orchestrator.validate_config"),
        patch("pycastle.orchestrator.STAGE_OVERRIDES", stage_overrides),
        patch("pycastle.orchestrator.run_agent", side_effect=_fake_run_agent),
        patch("pycastle.orchestrator.parse_plan", return_value=[]),
    ):
        asyncio.run(run({}, tmp_path))

    planner_call = next(c for c in captured if c["name"] == "Planner")
    assert planner_call["model"] == "claude-haiku-4-5"
    assert planner_call["effort"] == "low"


def test_implementer_receives_implement_stage_model_and_effort(tmp_path):
    """Each Implementer run_agent call must include model and effort from implement stage."""
    import asyncio
    from pycastle.orchestrator import run

    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append(
            {"name": name, "model": kwargs.get("model"), "effort": kwargs.get("effort")}
        )
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return "<plan>placeholder</plan>"

    stage_overrides = {
        "plan": {"model": "", "effort": ""},
        "implement": {"model": "claude-sonnet-4-6", "effort": "high"},
        "review": {"model": "", "effort": ""},
        "merge": {"model": "", "effort": ""},
    }

    with (
        patch("pycastle.orchestrator.prune_orphan_worktrees"),
        patch("pycastle.orchestrator.validate_config"),
        patch("pycastle.orchestrator.STAGE_OVERRIDES", stage_overrides),
        patch("pycastle.orchestrator.run_agent", side_effect=_fake_run_agent),
        patch(
            "pycastle.orchestrator.parse_plan",
            return_value=[{"number": 1, "title": "Fix", "branch": "issue/1"}],
        ),
    ):
        with _merge_patches():
            asyncio.run(run({}, tmp_path))

    impl_call = next(c for c in captured if "Implementer" in c["name"])
    assert impl_call["model"] == "claude-sonnet-4-6"
    assert impl_call["effort"] == "high"


def test_reviewer_receives_review_stage_model_and_effort(tmp_path):
    """Each Reviewer run_agent call must include model and effort from review stage."""
    import asyncio
    from pycastle.orchestrator import run

    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append(
            {"name": name, "model": kwargs.get("model"), "effort": kwargs.get("effort")}
        )
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return "<plan>placeholder</plan>"

    stage_overrides = {
        "plan": {"model": "", "effort": ""},
        "implement": {"model": "", "effort": ""},
        "review": {"model": "claude-haiku-4-5", "effort": "normal"},
        "merge": {"model": "", "effort": ""},
    }

    with (
        patch("pycastle.orchestrator.prune_orphan_worktrees"),
        patch("pycastle.orchestrator.validate_config"),
        patch("pycastle.orchestrator.STAGE_OVERRIDES", stage_overrides),
        patch("pycastle.orchestrator.run_agent", side_effect=_fake_run_agent),
        patch(
            "pycastle.orchestrator.parse_plan",
            return_value=[{"number": 1, "title": "Fix", "branch": "issue/1"}],
        ),
    ):
        with _merge_patches():
            asyncio.run(run({}, tmp_path))

    rev_call = next(c for c in captured if "Reviewer" in c["name"])
    assert rev_call["model"] == "claude-haiku-4-5"
    assert rev_call["effort"] == "normal"


def test_merger_receives_merge_stage_model_and_effort(tmp_path):
    """Merger run_agent call must include model and effort from merge stage override."""
    import asyncio
    from pycastle.orchestrator import run

    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append(
            {"name": name, "model": kwargs.get("model"), "effort": kwargs.get("effort")}
        )
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return "<plan>placeholder</plan>"

    stage_overrides = {
        "plan": {"model": "", "effort": ""},
        "implement": {"model": "", "effort": ""},
        "review": {"model": "", "effort": ""},
        "merge": {"model": "claude-opus-4-7", "effort": "low"},
    }

    with (
        patch("pycastle.orchestrator.prune_orphan_worktrees"),
        patch("pycastle.orchestrator.validate_config"),
        patch("pycastle.orchestrator.STAGE_OVERRIDES", stage_overrides),
        patch("pycastle.orchestrator.run_agent", side_effect=_fake_run_agent),
        patch(
            "pycastle.orchestrator.parse_plan",
            return_value=[{"number": 1, "title": "Fix", "branch": "issue/1"}],
        ),
    ):
        with _merge_patches(try_merge_return=False):
            asyncio.run(run({}, tmp_path))

    merger_call = next(c for c in captured if c["name"] == "Merger")
    assert merger_call["model"] == "claude-opus-4-7"
    assert merger_call["effort"] == "low"


def test_empty_stage_override_passes_empty_strings(tmp_path):
    """Empty model and effort in stage override must pass empty strings to run_agent."""
    import asyncio
    from pycastle.orchestrator import run

    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append(
            {"name": name, "model": kwargs.get("model"), "effort": kwargs.get("effort")}
        )
        return "<plan>[]</plan>"

    stage_overrides = {
        "plan": {"model": "", "effort": ""},
        "implement": {"model": "", "effort": ""},
        "review": {"model": "", "effort": ""},
        "merge": {"model": "", "effort": ""},
    }

    with (
        patch("pycastle.orchestrator.prune_orphan_worktrees"),
        patch("pycastle.orchestrator.validate_config"),
        patch("pycastle.orchestrator.STAGE_OVERRIDES", stage_overrides),
        patch("pycastle.orchestrator.run_agent", side_effect=_fake_run_agent),
        patch("pycastle.orchestrator.parse_plan", return_value=[]),
    ):
        asyncio.run(run({}, tmp_path))

    planner_call = next(c for c in captured if c["name"] == "Planner")
    assert planner_call["model"] == ""
    assert planner_call["effort"] == ""


def test_stage_overrides_are_independent(tmp_path):
    """Different stages must receive their own independent model/effort values."""
    import asyncio
    from pycastle.orchestrator import run

    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append(
            {"name": name, "model": kwargs.get("model"), "effort": kwargs.get("effort")}
        )
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return "<plan>placeholder</plan>"

    stage_overrides = {
        "plan": {"model": "claude-haiku-4-5", "effort": "low"},
        "implement": {"model": "claude-sonnet-4-6", "effort": "normal"},
        "review": {"model": "claude-haiku-4-5", "effort": ""},
        "merge": {"model": "claude-opus-4-7", "effort": "high"},
    }

    with (
        patch("pycastle.orchestrator.prune_orphan_worktrees"),
        patch("pycastle.orchestrator.validate_config"),
        patch("pycastle.orchestrator.STAGE_OVERRIDES", stage_overrides),
        patch("pycastle.orchestrator.run_agent", side_effect=_fake_run_agent),
        patch(
            "pycastle.orchestrator.parse_plan",
            return_value=[{"number": 1, "title": "Fix", "branch": "issue/1"}],
        ),
    ):
        with _merge_patches(try_merge_return=False):
            asyncio.run(run({}, tmp_path))

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
    import asyncio
    from pycastle.defaults.config import PREFLIGHT_CHECKS
    from pycastle.orchestrator import run

    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append({"name": name, "prompt_args": kwargs.get("prompt_args", {})})
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return "<plan>placeholder</plan>"

    with (
        patch("pycastle.orchestrator.prune_orphan_worktrees"),
        patch("pycastle.orchestrator.validate_config"),
        patch("pycastle.orchestrator.run_agent", side_effect=_fake_run_agent),
        patch(
            "pycastle.orchestrator.parse_plan",
            return_value=[{"number": 1, "title": "Fix", "branch": "issue/1"}],
        ),
    ):
        with _merge_patches(try_merge_return=False):
            asyncio.run(run({}, tmp_path))

    merger_call = next(c for c in captured if c["name"] == "Merger")
    expected_checks = " && ".join(cmd for _, cmd in PREFLIGHT_CHECKS)
    assert merger_call["prompt_args"]["CHECKS"] == expected_checks


def test_each_agent_passes_correct_stage_string(tmp_path):
    """Planner, Implementer, Reviewer, and Merger must each pass the correct stage= string."""
    import asyncio
    from pycastle.orchestrator import run

    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append({"name": name, "stage": kwargs.get("stage")})
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return "<plan>placeholder</plan>"

    with (
        patch("pycastle.orchestrator.prune_orphan_worktrees"),
        patch("pycastle.orchestrator.validate_config"),
        patch("pycastle.orchestrator.run_agent", side_effect=_fake_run_agent),
        patch(
            "pycastle.orchestrator.parse_plan",
            return_value=[{"number": 1, "title": "Fix", "branch": "issue/1"}],
        ),
    ):
        with _merge_patches(try_merge_return=False):
            asyncio.run(run({}, tmp_path))

    by_name = {c["name"]: c for c in captured}
    assert by_name["Planner"]["stage"] == "pre-planning"
    assert by_name["Implementer #1"]["stage"] == "pre-implementation"
    assert by_name["Reviewer #1"]["stage"] == "pre-review"
    assert by_name["Merger"]["stage"] == "pre-merge"


# ── Issue-95: parallel implementers with bounded concurrency ──────────────────


def test_multiple_implementers_run_in_parallel(tmp_path):
    """With MAX_PARALLEL >= N issues, all N implementers must be active simultaneously."""
    import asyncio
    from pycastle.orchestrator import run

    active_implementers: set[str] = set()
    max_concurrent = 0

    async def _fake_run_agent(
        name, prompt_file, mount_path, env, prompt_args=None, **kw
    ):
        nonlocal max_concurrent
        if name == "Planner":
            return "<plan>placeholder</plan>"
        if "Implementer" in name:
            active_implementers.add(name)
            max_concurrent = max(max_concurrent, len(active_implementers))
            await asyncio.sleep(0.05)
            active_implementers.discard(name)
            return "<promise>COMPLETE</promise>"
        return ""

    issues = [
        {"number": i, "title": f"Issue {i}", "branch": f"issue/{i}"}
        for i in range(1, 4)
    ]

    with (
        patch("pycastle.orchestrator.prune_orphan_worktrees"),
        patch("pycastle.orchestrator.validate_config"),
        patch("pycastle.orchestrator.run_agent", side_effect=_fake_run_agent),
        patch("pycastle.orchestrator.parse_plan", return_value=issues),
        patch("pycastle.orchestrator.MAX_PARALLEL", 4),
    ):
        with _merge_patches():
            asyncio.run(run({}, tmp_path))

    assert max_concurrent == 3, (
        f"Expected all 3 implementers active simultaneously, max was {max_concurrent}"
    )


def test_concurrent_agents_never_exceed_max_parallel(tmp_path):
    """The total number of concurrently active agents must never exceed MAX_PARALLEL."""
    import asyncio
    from pycastle.orchestrator import run

    active_count = 0
    max_active = 0
    max_parallel = 3

    async def _fake_run_agent(
        name, prompt_file, mount_path, env, prompt_args=None, **kw
    ):
        nonlocal active_count, max_active
        if name == "Planner":
            return "<plan>placeholder</plan>"
        active_count += 1
        max_active = max(max_active, active_count)
        await asyncio.sleep(0.01)
        active_count -= 1
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return ""

    issues = [
        {"number": i, "title": f"Issue {i}", "branch": f"issue/{i}"}
        for i in range(1, 8)
    ]

    with (
        patch("pycastle.orchestrator.prune_orphan_worktrees"),
        patch("pycastle.orchestrator.validate_config"),
        patch("pycastle.orchestrator.run_agent", side_effect=_fake_run_agent),
        patch("pycastle.orchestrator.parse_plan", return_value=issues),
        patch("pycastle.orchestrator.MAX_PARALLEL", max_parallel),
    ):
        with _merge_patches():
            asyncio.run(run({}, tmp_path))

    assert max_active <= max_parallel, (
        f"Active agents exceeded MAX_PARALLEL={max_parallel}: max observed={max_active}"
    )


def test_implementer_starts_while_reviewer_runs(tmp_path):
    """A new Implementer must be able to start while a prior issue's Reviewer is running."""
    import asyncio
    from pycastle.orchestrator import run

    events: list[str] = []

    async def _fake_run_agent(
        name, prompt_file, mount_path, env, prompt_args=None, **kw
    ):
        if name == "Planner":
            return "<plan>placeholder</plan>"
        events.append(f"start:{name}")
        await asyncio.sleep(0.03)
        events.append(f"end:{name}")
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return ""

    issues = [
        {"number": i, "title": f"Issue {i}", "branch": f"issue/{i}"}
        for i in range(1, 4)
    ]

    with (
        patch("pycastle.orchestrator.prune_orphan_worktrees"),
        patch("pycastle.orchestrator.validate_config"),
        patch("pycastle.orchestrator.run_agent", side_effect=_fake_run_agent),
        patch("pycastle.orchestrator.parse_plan", return_value=issues),
        patch("pycastle.orchestrator.MAX_PARALLEL", 2),
    ):
        with _merge_patches():
            asyncio.run(run({}, tmp_path))

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


def _make_merge_test_patches(
    tmp_path,
    *,
    issues: list[dict] | None = None,
    try_merge_side_effect=None,
    run_host_checks_return=None,
    extra_patches: list | None = None,
):
    """Return a context-manager stack with standard patches for merge-loop tests."""
    import contextlib

    issues = issues or [{"number": 1, "title": "Fix", "branch": "issue/1"}]
    if try_merge_side_effect is None:
        try_merge_side_effect = [True] * len(issues)

    mock_git = MagicMock()
    _results = list(try_merge_side_effect)
    _idx = [0]

    def _try_merge(repo_path, branch):
        val = _results[_idx[0]]
        _idx[0] += 1
        return val

    mock_git.try_merge.side_effect = _try_merge
    mock_github = MagicMock()

    @contextlib.contextmanager
    def _stack(run_agent_side_effect):
        patches = [
            patch("pycastle.orchestrator.prune_orphan_worktrees"),
            patch("pycastle.orchestrator.validate_config"),
            patch("pycastle.orchestrator.run_agent", side_effect=run_agent_side_effect),
            patch("pycastle.orchestrator.parse_plan", return_value=issues),
            patch("pycastle.orchestrator.GitService", return_value=mock_git),
            patch("pycastle.orchestrator.GithubService", return_value=mock_github),
            patch("pycastle.orchestrator._get_repo", return_value="owner/repo"),
            patch(
                "pycastle.orchestrator._run_host_checks",
                return_value=run_host_checks_return or [],
            ),
            patch("pycastle.orchestrator.MAX_ITERATIONS", 1),
        ]
        for p in extra_patches or []:
            patches.append(p)
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            yield mock_git, mock_github

    return _stack


def test_clean_merges_skip_merger(tmp_path):
    """When all branches merge cleanly, Merger agent must NOT be spawned."""
    import asyncio
    from pycastle.orchestrator import run

    agent_names: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        agent_names.append(name)
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return "<plan>placeholder</plan>"

    issues = [
        {"number": 1, "title": "Fix A", "branch": "issue/1"},
        {"number": 2, "title": "Fix B", "branch": "issue/2"},
    ]
    ctx = _make_merge_test_patches(
        tmp_path, issues=issues, try_merge_side_effect=[True, True]
    )
    with ctx(_fake_run_agent):
        asyncio.run(run({}, tmp_path))

    assert "Merger" not in agent_names, (
        f"Merger must not be spawned on clean merges; agents called: {agent_names}"
    )


def test_clean_merge_calls_close_issue_with_parents(tmp_path):
    """Each cleanly-merged issue must be closed via close_issue_with_parents."""
    import asyncio
    from pycastle.orchestrator import run

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return "<plan>placeholder</plan>"

    issues = [
        {"number": 7, "title": "Fix A", "branch": "issue/7"},
        {"number": 8, "title": "Fix B", "branch": "issue/8"},
    ]
    ctx = _make_merge_test_patches(
        tmp_path, issues=issues, try_merge_side_effect=[True, True]
    )
    with ctx(_fake_run_agent) as (mock_git, mock_github):
        asyncio.run(run({}, tmp_path))

    closed = [
        call.args[0] for call in mock_github.close_issue_with_parents.call_args_list
    ]
    assert sorted(closed) == [7, 8], f"Expected issues 7 and 8 closed; got {closed}"


def test_conflict_branch_spawns_merger_with_only_failing_branch(tmp_path):
    """When one branch conflicts, Merger is spawned with only the conflicting branch."""
    import asyncio
    from pycastle.orchestrator import run

    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append({"name": name, "prompt_args": kwargs.get("prompt_args", {})})
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return "<plan>placeholder</plan>"

    issues = [
        {"number": 1, "title": "Clean", "branch": "issue/1"},
        {"number": 2, "title": "Conflict", "branch": "issue/2"},
    ]
    # issue/1 merges cleanly, issue/2 conflicts
    ctx = _make_merge_test_patches(
        tmp_path, issues=issues, try_merge_side_effect=[True, False]
    )
    with ctx(_fake_run_agent):
        asyncio.run(run({}, tmp_path))

    merger_calls = [c for c in captured if c["name"] == "Merger"]
    assert len(merger_calls) == 1, (
        f"Expected exactly one Merger call; got {merger_calls}"
    )
    branches_arg = merger_calls[0]["prompt_args"]["BRANCHES"]
    assert "issue/2" in branches_arg
    assert "issue/1" not in branches_arg


def test_conflict_branch_skips_post_merge_checks(tmp_path):
    """When any branch conflicts, post-merge host checks must not run."""
    import asyncio
    from pycastle.orchestrator import run

    host_checks_called = []

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return "<plan>placeholder</plan>"

    issues = [{"number": 1, "title": "Conflict", "branch": "issue/1"}]
    ctx = _make_merge_test_patches(
        tmp_path, issues=issues, try_merge_side_effect=[False]
    )
    with ctx(_fake_run_agent):
        with patch(
            "pycastle.orchestrator._run_host_checks",
            side_effect=lambda _: host_checks_called.append(True) or [],
        ):
            asyncio.run(run({}, tmp_path))

    assert host_checks_called == [], "Host checks must not run when conflicts exist"


def test_post_merge_checks_run_after_all_clean_merges(tmp_path):
    """After all clean merges with no conflicts, host PREFLIGHT_CHECKS must run."""
    import asyncio
    from pycastle.orchestrator import run

    host_checks_called = []

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return "<plan>placeholder</plan>"

    issues = [{"number": 1, "title": "Fix", "branch": "issue/1"}]

    ctx = _make_merge_test_patches(
        tmp_path, issues=issues, try_merge_side_effect=[True]
    )
    with ctx(_fake_run_agent):
        with patch(
            "pycastle.orchestrator._run_host_checks",
            side_effect=lambda checks: host_checks_called.append(checks) or [],
        ):
            asyncio.run(run({}, tmp_path))

    assert len(host_checks_called) == 1, (
        f"_run_host_checks must be called once; called {len(host_checks_called)} times"
    )


def test_post_merge_check_failure_spawns_bug_report_not_merger(tmp_path):
    """On post-merge check failure, bug-report is spawned and Merger is NOT spawned."""
    import asyncio
    from pycastle.orchestrator import run

    agent_names: list[str] = []

    async def _fake_run_agent(name, **kwargs):
        agent_names.append(name)
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return "<plan>placeholder</plan>"

    issues = [{"number": 1, "title": "Fix", "branch": "issue/1"}]
    failures = [("pytest", "pytest", "FAILED tests/test_foo.py")]
    ctx = _make_merge_test_patches(
        tmp_path,
        issues=issues,
        try_merge_side_effect=[True],
        run_host_checks_return=failures,
    )
    with ctx(_fake_run_agent):
        asyncio.run(run({}, tmp_path))

    assert "Merger" not in agent_names, (
        f"Merger must not spawn on check failure; agents={agent_names}"
    )
    bug_report_names = [n for n in agent_names if "bug-report" in n]
    assert len(bug_report_names) >= 1, (
        f"At least one bug-report agent expected; agents={agent_names}"
    )


def test_post_merge_bug_report_uses_post_merge_stage(tmp_path):
    """Bug-report agents spawned on post-merge failure must use '[post-merge]' in CHECK_NAME."""
    import asyncio
    from pycastle.orchestrator import run

    captured: list[dict] = []

    async def _fake_run_agent(name, **kwargs):
        captured.append({"name": name, "prompt_args": kwargs.get("prompt_args", {})})
        if "Implementer" in name:
            return "<promise>COMPLETE</promise>"
        return "<plan>placeholder</plan>"

    issues = [{"number": 1, "title": "Fix", "branch": "issue/1"}]
    failures = [("pytest", "pytest", "FAILED")]
    ctx = _make_merge_test_patches(
        tmp_path,
        issues=issues,
        try_merge_side_effect=[True],
        run_host_checks_return=failures,
    )
    with ctx(_fake_run_agent):
        asyncio.run(run({}, tmp_path))

    bug_calls = [c for c in captured if "bug-report" in c["name"]]
    assert bug_calls, "Expected bug-report agent calls"
    check_name = bug_calls[0]["prompt_args"].get("CHECK_NAME", "")
    assert "[post-merge]" in check_name, (
        f"CHECK_NAME must include '[post-merge]'; got {check_name!r}"
    )
