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
