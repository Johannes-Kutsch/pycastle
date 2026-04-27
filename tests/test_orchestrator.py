from pathlib import Path
from unittest.mock import patch


from pycastle.orchestrator import prune_orphan_worktrees


# ── Cycle 24-B1: prune_orphan_worktrees deletes orphan dirs ──────────────────


def _porcelain(paths: list[Path]) -> str:
    """Build a git worktree list --porcelain string listing the given paths."""
    lines = []
    for p in paths:
        lines.append(f"worktree {p}")
        lines.append("HEAD abc1234")
        lines.append("branch refs/heads/main")
        lines.append("")
    return "\n".join(lines)


def test_prune_orphan_worktrees_deletes_absent_dir(tmp_path):
    worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)
    orphan = worktrees_dir / "orphan-branch"
    orphan.mkdir()

    with patch("subprocess.check_output", return_value=_porcelain([])):
        prune_orphan_worktrees(tmp_path)

    assert not orphan.exists()


def test_prune_orphan_worktrees_deletes_only_orphans(tmp_path):
    """Only dirs absent from git worktree list must be deleted; active ones survive."""
    worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)
    orphan = worktrees_dir / "orphan"
    orphan.mkdir()
    active = worktrees_dir / "active-branch"
    active.mkdir()

    with patch("subprocess.check_output", return_value=_porcelain([active])):
        prune_orphan_worktrees(tmp_path)

    assert not orphan.exists()
    assert active.exists()


# ── Cycle 24-B2: prune_orphan_worktrees preserves active worktrees ───────────


def test_prune_orphan_worktrees_preserves_active_dir(tmp_path):
    worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)
    active = worktrees_dir / "my-branch"
    active.mkdir()

    with patch("subprocess.check_output", return_value=_porcelain([active])):
        prune_orphan_worktrees(tmp_path)

    assert active.exists()


def test_prune_orphan_worktrees_noop_when_dir_missing(tmp_path):
    """Must not raise if pycastle/.worktrees/ does not exist yet."""
    with patch("subprocess.check_output", return_value=_porcelain([])):
        prune_orphan_worktrees(tmp_path)  # no exception


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
