import pytest
from unittest.mock import MagicMock
import asyncio
import platform
import shlex
import subprocess
import sys
from pathlib import Path

from pycastle._host_check import HostCheckCommandResult
from pycastle.agents.output_protocol import AgentRole
from pycastle.commands.host_check_run import (
    HostCheckRunPassed,
    prepare_host_check_run,
)
from pycastle.config import StageOverride
from pycastle.prompts.pipeline import PromptTemplate
from tests.support import FakeAgentRunner, RecordingStatusDisplay


def host_check_command_result(
    name: str,
    command: str,
    *,
    returncode: int = 0,
    output: str = "",
) -> HostCheckCommandResult:
    return HostCheckCommandResult(
        name=name,
        command=command,
        returncode=returncode,
        output=output,
    )


def test_prepare_host_check_run_refreshes_before_clean_tree_and_fails_early():
    from pycastle.commands import host_check_run as run_mod

    events: list[tuple[str, object]] = []
    git_svc = MagicMock()

    def fake_pull(repo_root):
        events.append(("pull", repo_root))

    def fake_clean(repo_root):
        events.append(("clean", repo_root))
        return False

    git_svc.pull_with_merge_fallback.side_effect = fake_pull
    git_svc.is_working_tree_clean.side_effect = fake_clean

    with pytest.raises(
        RuntimeError, match="Working tree must be clean before running host checks."
    ):
        run_mod.prepare_host_check_run(git_svc=git_svc)

    assert events == [
        ("pull", run_mod.Path(".").resolve()),
        ("clean", run_mod.Path(".").resolve()),
    ]
    git_svc.get_head_sha.assert_not_called()


def test_prepare_host_check_run_returns_head_sha_when_working_tree_is_clean():
    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "abc123"

    result = prepare_host_check_run(git_svc=git_svc)

    assert result == "abc123"


def test_prepare_host_check_run_passes_explicit_repo_root_to_git_service(tmp_path):
    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "def456"

    result = prepare_host_check_run(git_svc=git_svc, repo_root=tmp_path)

    git_svc.pull_with_merge_fallback.assert_called_once_with(tmp_path)
    git_svc.is_working_tree_clean.assert_called_once_with(tmp_path)
    git_svc.get_head_sha.assert_called_once_with(tmp_path)
    assert result == "def456"


def test_run_host_check_command_builds_default_issue_filing_deps_when_host_check_fails(
    tmp_path, monkeypatch
):
    from pycastle.agents.output_protocol import IssueOutput
    from pycastle.commands import host_check_run as run_mod
    from pycastle.config import Config

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "checked-sha"
    git_svc.get_github_remote_repo.return_value = ("owner", "repo")
    cfg = Config()
    check_name, check_command = cfg.host_checks[0]
    created_github_services: list[tuple[str, str, Config]] = []
    runner_requests = []

    monkeypatch.setattr(
        run_mod,
        "load_credential_env",
        lambda **kwargs: {"GH_TOKEN": "token"},
    )
    monkeypatch.setattr(run_mod, "_configured_service_registry", lambda cfg, env: {})

    class _FakeGithubService:
        def __init__(self, repo: str, token: str, cfg: Config) -> None:
            created_github_services.append((repo, token, cfg))
            self.repo = repo

        def get_issue(self, number: int) -> dict[str, str]:
            return {"body": "x" * 100}

    class _FakeAgentRunner:
        def __init__(self, env, cfg, git_svc, *, service_registry) -> None:
            self.calls = runner_requests

        async def run(self, request):
            self.calls.append(request)
            return IssueOutput(number=41, labels=["bug", "ready-for-human"])

    monkeypatch.setattr(run_mod, "GithubService", _FakeGithubService)
    monkeypatch.setattr(
        sys.modules["pycastle.agents.runner"], "AgentRunner", _FakeAgentRunner
    )

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    def fake_run_host_check(name: str, command: str, cwd: Path) -> None:
        raise run_mod.HostCheckFailedError(
            name=name,
            command=command,
            output="tests broke",
        )

    monkeypatch.setattr(
        run_mod, "transient_worktree", lambda *a, **kw: _TransientWorktree()
    )
    monkeypatch.setattr(run_mod, "_run_host_check", fake_run_host_check)

    result = asyncio.run(
        run_mod.run_host_check_command(
            cfg=cfg,
            git_svc=git_svc,
            repo_root=tmp_path,
        )
    )

    assert result == run_mod.HostCheckRunFailed(
        checked_sha="checked-sha",
        failures=(
            run_mod.HostCheckFailure(
                name=check_name,
                command=check_command,
                output="tests broke",
            ),
        ),
        issue_numbers=(41,),
    )
    assert created_github_services == [("owner/repo", "token", cfg)]
    assert len(runner_requests) == 1
    assert runner_requests[0].service == cfg.preflight_issue_override.service
    assert runner_requests[0].model == cfg.preflight_issue_override.model
    assert runner_requests[0].effort == cfg.preflight_issue_override.effort
    assert runner_requests[0].prompt.template == PromptTemplate.HOST_CHECK_ISSUE
    assert runner_requests[0].role == AgentRole.PREFLIGHT_ISSUE
    assert runner_requests[0].work_body == f"reporting {check_name} host-check issue"


def test_run_host_check_command_normalizes_in_memory_failed_command_outcome_through_module_seam(
    tmp_path, monkeypatch
):
    from pycastle.agents.output_protocol import IssueOutput
    from pycastle.commands import host_check_run as run_mod
    from pycastle.config import Config

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "checked-sha"
    git_svc.get_github_remote_repo.return_value = ("owner", "repo")
    cfg = Config()
    check_name, check_command = cfg.host_checks[0]
    runner_requests = []

    monkeypatch.setattr(
        run_mod,
        "load_credential_env",
        lambda **kwargs: {"GH_TOKEN": "token"},
    )
    monkeypatch.setattr(run_mod, "_configured_service_registry", lambda cfg, env: {})

    class _FakeGithubService:
        def __init__(self, repo: str, token: str, cfg: Config) -> None:
            self.repo = repo

        def get_issue(self, number: int) -> dict[str, str]:
            return {"body": "x" * 100}

    class _FakeAgentRunner:
        def __init__(self, env, cfg, git_svc, *, service_registry) -> None:
            self.calls = runner_requests

        async def run(self, request):
            self.calls.append(request)
            return IssueOutput(number=41, labels=["bug", "ready-for-human"])

    monkeypatch.setattr(run_mod, "GithubService", _FakeGithubService)
    monkeypatch.setattr(
        sys.modules["pycastle.agents.runner"], "AgentRunner", _FakeAgentRunner
    )

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    result = asyncio.run(
        run_mod.run_host_check_command(
            cfg=cfg,
            git_svc=git_svc,
            repo_root=tmp_path,
            run_host_check=lambda name, command, cwd: host_check_command_result(
                name,
                command,
                returncode=1,
                output="stdout line\nstderr line",
            ),
            transient_worktree_factory=lambda *a, **kw: _TransientWorktree(),
        )
    )

    assert result == run_mod.HostCheckRunFailed(
        checked_sha="checked-sha",
        failures=(
            run_mod.HostCheckFailure(
                name=check_name,
                command=check_command,
                output="stdout line\nstderr line",
            ),
        ),
        issue_numbers=(41,),
    )
    assert len(runner_requests) == 1
    assert runner_requests[0].work_body == f"reporting {check_name} host-check issue"


def test_run_host_check_command_uses_service_registry_for_reporter_override(
    tmp_path, monkeypatch
):
    from pycastle.commands import host_check_run as run_mod
    from pycastle.config import Config

    git_svc = MagicMock()
    status_display = RecordingStatusDisplay()
    cfg = Config()
    provided_service_registry = MagicMock()
    github_svc = MagicMock()
    agent_runner = MagicMock()
    reporter_override = StageOverride(
        service="codex",
        model="gpt-5.4-mini",
        effort="medium",
    )
    provided_service_registry.resolve.return_value = reporter_override

    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "checked-sha"
    github_svc.get_issue.return_value = {"body": "x" * 100}
    agent_runner = FakeAgentRunner(
        [run_mod.IssueOutput(number=41, labels=["bug", "ready-for-human"])]
    )

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    def fake_run_host_check(name: str, command: str, cwd: Path) -> None:
        raise run_mod.HostCheckFailedError(
            name=name,
            command=command,
            output="tests broke",
        )

    monkeypatch.setattr(
        run_mod, "transient_worktree", lambda *a, **kw: _TransientWorktree()
    )
    monkeypatch.setattr(run_mod, "_run_host_check", fake_run_host_check)

    result = asyncio.run(
        run_mod.run_host_check_command(
            cfg=cfg,
            git_svc=git_svc,
            repo_root=tmp_path,
            github_svc=github_svc,
            agent_runner=agent_runner,
            status_display=status_display,
            service_registry=provided_service_registry,
        )
    )

    assert result.issue_numbers == (41,)
    assert agent_runner.calls[0].service == reporter_override.service
    assert agent_runner.calls[0].model == reporter_override.model
    assert agent_runner.calls[0].effort == reporter_override.effort
    provided_service_registry.resolve.assert_called_once()


def test_run_host_check_command_preserves_raw_failed_command_diagnostic_payload(
    tmp_path, monkeypatch
):
    from pycastle.agents.output_protocol import IssueOutput
    from pycastle.commands import host_check_run as run_mod
    from pycastle.config import Config

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "checked-sha"
    git_svc.get_github_remote_repo.return_value = ("owner", "repo")
    cfg = Config()
    check_name, check_command = cfg.host_checks[0]
    runner_requests = []
    raw_output = "\nstdout line\nstderr line\n"

    monkeypatch.setattr(
        run_mod,
        "load_credential_env",
        lambda **kwargs: {"GH_TOKEN": "token"},
    )
    monkeypatch.setattr(run_mod, "_configured_service_registry", lambda cfg, env: {})

    class _FakeGithubService:
        def __init__(self, repo: str, token: str, cfg: Config) -> None:
            self.repo = repo

        def get_issue(self, number: int) -> dict[str, str]:
            return {"body": "x" * 100}

    class _FakeAgentRunner:
        def __init__(self, env, cfg, git_svc, *, service_registry) -> None:
            self.calls = runner_requests

        async def run(self, request):
            self.calls.append(request)
            return IssueOutput(number=41, labels=["bug", "ready-for-human"])

    monkeypatch.setattr(run_mod, "GithubService", _FakeGithubService)
    monkeypatch.setattr(
        sys.modules["pycastle.agents.runner"], "AgentRunner", _FakeAgentRunner
    )

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    result = asyncio.run(
        run_mod.run_host_check_command(
            cfg=cfg,
            git_svc=git_svc,
            repo_root=tmp_path,
            run_host_check=lambda name, command, cwd: host_check_command_result(
                name,
                command,
                returncode=1,
                output=raw_output,
            ),
            transient_worktree_factory=lambda *a, **kw: _TransientWorktree(),
        )
    )

    assert result == run_mod.HostCheckRunFailed(
        checked_sha="checked-sha",
        failures=(
            run_mod.HostCheckFailure(
                name=check_name,
                command=check_command,
                output=raw_output,
            ),
        ),
        issue_numbers=(41,),
    )
    assert runner_requests[0].prompt.scope_args["OUTPUT"] == raw_output


def test_run_host_check_command_uses_in_memory_reporter_adapters_with_exact_host_check_scope_args(
    tmp_path, monkeypatch
):
    from pycastle.agents.output_protocol import IssueOutput
    from pycastle.commands import host_check_run as run_mod
    from pycastle.config import Config

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "checked-sha"
    cfg = Config(host_checks=(("pytest-host", "pytest tests/host"),))
    status_display = RecordingStatusDisplay()
    raw_output = "\nstdout line\nstderr line\n"

    monkeypatch.setattr(
        "pycastle._host_check.platform.system",
        lambda: "HostOS",
    )
    monkeypatch.setattr(
        "pycastle._host_check.platform.platform",
        lambda: "HostOS-1.0",
    )
    monkeypatch.setattr(
        run_mod,
        "resolve_host_check_issue_deps",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("production host-check issue deps should not be resolved")
        ),
    )
    original_build_host_check_scope_args = (
        run_mod.prompt_scope_args.build_host_check_scope_args
    )
    captured_scope_kwargs: list[dict[str, str]] = []

    def capture_host_check_scope_args(**kwargs):
        captured_scope_kwargs.append(kwargs)
        return original_build_host_check_scope_args(**kwargs)

    monkeypatch.setattr(
        run_mod.prompt_scope_args,
        "build_host_check_scope_args",
        capture_host_check_scope_args,
    )

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    agent_runner = FakeAgentRunner(
        [IssueOutput(number=41, labels=["bug", "ready-for-human"])]
    )
    github_svc = MagicMock()
    github_svc.get_issue.return_value = {"body": "x" * 100}

    result = asyncio.run(
        run_mod.run_host_check_command(
            cfg=cfg,
            git_svc=git_svc,
            repo_root=tmp_path,
            github_svc=github_svc,
            agent_runner=agent_runner,
            status_display=status_display,
            run_host_check=lambda name, command, cwd: host_check_command_result(
                name,
                command,
                returncode=1,
                output=raw_output,
            ),
            transient_worktree_factory=lambda *a, **kw: _TransientWorktree(),
        )
    )

    assert result == run_mod.HostCheckRunFailed(
        checked_sha="checked-sha",
        failures=(
            run_mod.HostCheckFailure(
                name="pytest-host",
                command="pytest tests/host",
                output=raw_output,
            ),
        ),
        issue_numbers=(41,),
    )
    assert len(agent_runner.calls) == 1
    assert agent_runner.calls[0].prompt.template == PromptTemplate.HOST_CHECK_ISSUE
    assert agent_runner.calls[0].role == AgentRole.PREFLIGHT_ISSUE
    assert captured_scope_kwargs == [
        {
            "checked_sha": "checked-sha",
            "check_name": "pytest-host",
            "command": "pytest tests/host",
            "output": raw_output,
            "host_os": "HostOS",
            "host_platform": "HostOS-1.0",
        }
    ]
    assert agent_runner.calls[0].prompt.scope_args == {
        "HOST_OS": "HostOS",
        "HOST_PLATFORM": "HostOS-1.0",
        "CHECKED_SHA": "checked-sha",
        "CHECK_NAME": "pytest-host",
        "COMMAND": "pytest tests/host",
        "OUTPUT": raw_output,
    }


def test_run_host_check_command_executes_configured_passing_check_through_module_seam_without_streaming_output(
    tmp_path, capsys
):
    from pycastle.commands import host_check_run as run_mod
    from pycastle.config import Config

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "checked-sha"
    cfg = Config(host_checks=(("configured", "python -c configured"),))

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    result = asyncio.run(
        run_mod.run_host_check_command(
            cfg=cfg,
            git_svc=git_svc,
            repo_root=tmp_path,
            status_display=run_mod.PlainStatusDisplay(),
            run_host_check=lambda name, command, cwd: host_check_command_result(
                name,
                command,
                output="passing stdout\npassing stderr",
            ),
            transient_worktree_factory=lambda *a, **kw: _TransientWorktree(),
        )
    )

    assert result == run_mod.HostCheckRunPassed(checked_sha="checked-sha")
    assert capsys.readouterr().out == (
        "\n[Host Check] started\n[Host Check] configured\n[Host Check] finished\n"
    )


def test_run_host_check_command_returns_passed_verdict_when_passing_adapter_only_raises_on_failure(
    tmp_path,
):
    from pycastle.commands import host_check_run as run_mod
    from pycastle.config import Config

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "checked-sha"
    cfg = Config(host_checks=(("configured", "python -c configured"),))

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    result = asyncio.run(
        run_mod.run_host_check_command(
            cfg=cfg,
            git_svc=git_svc,
            repo_root=tmp_path,
            run_host_check=lambda name, command, cwd: None,
            transient_worktree_factory=lambda *a, **kw: _TransientWorktree(),
        )
    )

    assert result == run_mod.HostCheckRunPassed(checked_sha="checked-sha")


def test_run_host_check_run_does_not_resolve_host_check_issue_deps_when_checks_pass(
    tmp_path,
):
    from pycastle.commands import host_check_run as run_mod

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "checked-sha"
    issue_deps_factory = MagicMock()

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    result = asyncio.run(
        run_mod.run_host_check_run(
            host_checks=(("configured", "python -c configured"),),
            git_svc=git_svc,
            repo_root=tmp_path,
            issue_deps_factory=issue_deps_factory,
            run_host_check=lambda name, command, cwd: None,
            transient_worktree_factory=lambda *a, **kw: _TransientWorktree(),
        )
    )

    assert result == run_mod.HostCheckRunPassed(checked_sha="checked-sha")
    issue_deps_factory.assert_not_called()


def test_run_host_check_run_executes_passing_checks_in_checked_sha_worktree_and_returns_sha(
    tmp_path, monkeypatch, capsys
):
    from pycastle.commands import host_check_run as run_mod

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "checked-sha"

    (tmp_path / "checked.txt").write_text("fresh\n", encoding="utf-8")

    script = (
        "from pathlib import Path; "
        "assert Path('checked.txt').read_text() == 'fresh\\n'; "
        "print('passing stdout'); "
        "import sys; print('passing stderr', file=sys.stderr)"
    )
    if platform.system() == "Windows":
        command = subprocess.list2cmdline([sys.executable, "-c", script])
    else:
        command = shlex.join([sys.executable, "-c", script])

    transient_calls: list[tuple[str, str, Path]] = []
    surfaced: list[str] = []

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            transient_calls.append(("enter", "checked-sha", tmp_path))
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            transient_calls.append(("exit", "checked-sha", tmp_path))
            return None

    def fake_transient_worktree(name: str, *, sha: str | None, deps):
        assert name == "host-check-checked"
        assert sha == "checked-sha"
        assert deps.repo_root == tmp_path
        assert deps.git_svc is git_svc
        return _TransientWorktree()

    monkeypatch.setattr(run_mod, "transient_worktree", fake_transient_worktree)

    result = asyncio.run(
        run_mod.run_host_check_run(
            host_checks=(("freshness", command),),
            git_svc=git_svc,
            repo_root=tmp_path,
            on_check_start=surfaced.append,
        )
    )

    assert result == HostCheckRunPassed(checked_sha="checked-sha")
    assert surfaced == ["freshness"]
    assert transient_calls == [
        ("enter", "checked-sha", tmp_path),
        ("exit", "checked-sha", tmp_path),
    ]
    out = capsys.readouterr()
    assert "passing stdout" not in out.out
    assert "passing stderr" not in out.out


def test_run_host_check_run_surfaces_host_check_phase_row_before_worktree_steps(
    tmp_path,
):
    from pycastle.commands import host_check_run as run_mod

    events: list[tuple[object, ...]] = []
    git_svc = MagicMock()

    def fake_pull(repo_root: Path) -> None:
        events.append(("pull", repo_root))

    def fake_clean(repo_root: Path) -> bool:
        events.append(("clean", repo_root))
        return True

    git_svc.pull_with_merge_fallback.side_effect = fake_pull
    git_svc.is_working_tree_clean.side_effect = fake_clean
    git_svc.get_head_sha.return_value = "abc123def456"

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            events.append(("worktree-enter",))
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            events.append(("worktree-exit",))
            return None

    display = RecordingStatusDisplay()

    result = asyncio.run(
        run_mod.run_host_check_run(
            host_checks=(("tests", "python -c tests"),),
            git_svc=git_svc,
            repo_root=tmp_path,
            status_display=display,
            run_host_check=lambda name, command, cwd: (
                events.append(("host-check", name, command, cwd))
                or host_check_command_result(name, command)
            ),
            transient_worktree_factory=lambda *a, **kw: _TransientWorktree(),
        )
    )

    assert result == HostCheckRunPassed(checked_sha="abc123def456")
    assert display.calls[0] == (
        "register",
        "Host Check",
        "phase",
        "started",
        "Setup",
        None,
    )
    assert events[:3] == [
        ("pull", tmp_path),
        ("clean", tmp_path),
        ("worktree-enter",),
    ]
    assert ("host-check", "tests", "python -c tests", tmp_path) in events
    assert display.calls[-1] == ("remove", "Host Check", "finished", "success")


def test_run_host_check_run_names_current_host_check_through_status_surface(tmp_path):
    from pycastle.commands import host_check_run as run_mod

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "abc123def456"

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    display = RecordingStatusDisplay()

    asyncio.run(
        run_mod.run_host_check_run(
            host_checks=(
                ("lint", "python -c lint"),
                ("tests", "python -c tests"),
            ),
            git_svc=git_svc,
            repo_root=tmp_path,
            status_display=display,
            run_host_check=lambda name, command, cwd: host_check_command_result(
                name, command
            ),
            transient_worktree_factory=lambda *a, **kw: _TransientWorktree(),
        )
    )

    assert ("update_phase", "Host Check", "lint") in display.calls
    assert ("update_phase", "Host Check", "tests") in display.calls


def test_run_host_check_run_surfaces_current_host_check_without_streaming_passing_command_output(
    tmp_path, capsys
):
    from pycastle.commands import host_check_run as run_mod

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "abc123def456"

    script = (
        "import sys; print('passing stdout'); print('passing stderr', file=sys.stderr)"
    )
    if platform.system() == "Windows":
        command = subprocess.list2cmdline([sys.executable, "-c", script])
    else:
        command = shlex.join([sys.executable, "-c", script])

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    asyncio.run(
        run_mod.run_host_check_run(
            host_checks=(("noisy", command),),
            git_svc=git_svc,
            repo_root=tmp_path,
            status_display=run_mod.PlainStatusDisplay(),
            transient_worktree_factory=lambda *a, **kw: _TransientWorktree(),
        )
    )

    assert capsys.readouterr().out == (
        "\n[Host Check] started\n[Host Check] noisy\n[Host Check] finished\n"
    )


def test_run_host_check_run_keeps_clean_tree_abort_behavior_with_phase_row(capsys):
    from pycastle.commands import host_check_run as run_mod

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = False

    with pytest.raises(
        RuntimeError, match="Working tree must be clean before running host checks."
    ):
        asyncio.run(
            run_mod.run_host_check_run(
                host_checks=(("tests", "python -c tests"),),
                git_svc=git_svc,
                status_display=run_mod.PlainStatusDisplay(),
            )
        )

    assert capsys.readouterr().out == "\n[Host Check] started\n[Host Check] failed\n"
    git_svc.get_head_sha.assert_not_called()


def test_run_host_check_run_collects_structured_failed_checks_without_leaking_command_text(
    tmp_path,
):
    from pycastle.commands import host_check_run as run_mod

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "checked-sha"

    seen_checks: list[tuple[str, str, Path]] = []
    transient_shas: list[str] = []
    multi_line_command = "python -c lint\npython -c more-lint"

    def fake_run_host_check(
        name: str, command: str, cwd: Path
    ) -> HostCheckCommandResult:
        seen_checks.append((name, command, cwd))
        if name == "format":
            return host_check_command_result(name, command)
        return host_check_command_result(
            name,
            command,
            returncode=1,
            output=f"{name} stdout\n{name} stderr",
        )

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    def fake_transient_worktree(name: str, *, sha: str | None, deps):
        assert name == "host-check-checked"
        assert deps.repo_root == tmp_path
        transient_shas.append(sha or "")
        return _TransientWorktree()

    result = asyncio.run(
        run_mod.run_host_check_run(
            host_checks=(
                ("lint", multi_line_command),
                ("format", "python -c format"),
                ("tests", "python -c tests"),
            ),
            git_svc=git_svc,
            repo_root=tmp_path,
            run_host_check=fake_run_host_check,
            transient_worktree_factory=fake_transient_worktree,
        )
    )

    assert result == run_mod.HostCheckRunFailed(
        checked_sha="checked-sha",
        failures=(
            run_mod.HostCheckFailure(
                name="lint",
                command=multi_line_command,
                output="lint stdout\nlint stderr",
            ),
            run_mod.HostCheckFailure(
                name="tests",
                command="python -c tests",
                output="tests stdout\ntests stderr",
            ),
        ),
        issue_numbers=(),
    )
    assert seen_checks == [
        ("lint", multi_line_command, tmp_path),
        ("format", "python -c format", tmp_path),
        ("tests", "python -c tests", tmp_path),
    ]
    assert transient_shas == ["checked-sha"]


def test_run_host_check_run_surfaces_each_failed_host_check_before_host_check_reporter_startup(
    tmp_path, capsys
):
    from pycastle.agents.output_protocol import IssueOutput
    from pycastle.agents.runner import RunRequest
    from pycastle.commands import host_check_run as run_mod
    from pycastle.config import Config

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "checked-sha"

    failures = {
        "lint": run_mod.HostCheckFailedError(
            name="lint", command="python -c lint", output="lint broke"
        ),
        "tests": run_mod.HostCheckFailedError(
            name="tests", command="python -c tests", output="tests broke"
        ),
    }

    def fake_run_host_check(
        name: str, command: str, cwd: Path
    ) -> HostCheckCommandResult:
        exc = failures.get(name)
        if exc is not None:
            raise exc
        return host_check_command_result(name, command)

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    class _RecordingAgentRunner:
        def __init__(self) -> None:
            self.calls: list[RunRequest] = []

        async def run(self, request: RunRequest) -> IssueOutput:
            self.calls.append(request)
            request.status_display.register(request.name, "agent")
            request.status_display.remove(request.name)
            return IssueOutput(
                number=40 + len(self.calls), labels=["bug", "ready-for-human"]
            )

    runner = _RecordingAgentRunner()
    github_svc = MagicMock()

    result = asyncio.run(
        run_mod.run_host_check_run(
            host_checks=(
                ("lint", "python -c lint"),
                ("tests", "python -c tests"),
                ("format", "python -c format"),
            ),
            git_svc=git_svc,
            repo_root=tmp_path,
            cfg=Config(),
            github_svc=github_svc,
            agent_runner=runner,
            status_display=run_mod.PlainStatusDisplay(),
            run_host_check=fake_run_host_check,
            transient_worktree_factory=lambda *a, **kw: _TransientWorktree(),
        )
    )

    assert result == run_mod.HostCheckRunFailed(
        checked_sha="checked-sha",
        failures=(
            run_mod.HostCheckFailure(
                name="lint",
                command="python -c lint",
                output="lint broke",
            ),
            run_mod.HostCheckFailure(
                name="tests",
                command="python -c tests",
                output="tests broke",
            ),
        ),
        issue_numbers=(41, 42),
    )
    out = capsys.readouterr().out
    assert out.index("[Host Check] failed lint") < out.index(
        "[Host-Check Reporter] started"
    )
    assert out.index("[Host Check] failed tests") < out.index(
        "[Host-Check Reporter] started"
    )
    assert out.index("[Host Check] failed lint") < out.rindex(
        "[Host Check] failed lint"
    )
    assert out.rindex("[Host Check] failed lint") < out.index(
        "[Host-Check Reporter] started"
    )
    assert out.index("[Host Check] format") < out.index("[Host-Check Reporter] started")


def test_run_host_check_run_files_and_validates_one_issue_per_failed_check_in_order(
    tmp_path,
):
    from pycastle.agents.output_protocol import IssueOutput
    from pycastle.commands import host_check_run as run_mod
    from pycastle.config import Config
    from pycastle.prompts.pipeline import PromptTemplate

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "checked-sha"

    def fake_run_host_check(
        name: str, command: str, cwd: Path
    ) -> HostCheckCommandResult:
        if name == "format":
            return host_check_command_result(name, command)
        return host_check_command_result(
            name=name,
            command=command,
            returncode=1,
            output=f"{name} stdout\n{name} stderr",
        )

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    def fake_transient_worktree(name: str, *, sha: str | None, deps):
        assert name == "host-check-checked"
        assert sha == "checked-sha"
        assert deps.repo_root == tmp_path
        assert deps.git_svc is git_svc
        return _TransientWorktree()

    agent_runner = FakeAgentRunner(
        [
            IssueOutput(number=41, labels=["bug", "ready-for-human"]),
            IssueOutput(number=42, labels=["bug", "behavior-slice", "ready-for-agent"]),
        ]
    )
    github_svc = MagicMock()
    github_svc.get_issue.return_value = {"body": "x" * 100}
    status_display = MagicMock()

    result = asyncio.run(
        run_mod.run_host_check_run(
            host_checks=(
                ("lint", "python -c lint"),
                ("format", "python -c format"),
                ("tests", "python -c tests"),
            ),
            git_svc=git_svc,
            repo_root=tmp_path,
            cfg=Config(),
            github_svc=github_svc,
            agent_runner=agent_runner,
            status_display=status_display,
            run_host_check=fake_run_host_check,
            transient_worktree_factory=fake_transient_worktree,
        )
    )

    assert result == run_mod.HostCheckRunFailed(
        checked_sha="checked-sha",
        failures=(
            run_mod.HostCheckFailure(
                name="lint",
                command="python -c lint",
                output="lint stdout\nlint stderr",
            ),
            run_mod.HostCheckFailure(
                name="tests",
                command="python -c tests",
                output="tests stdout\ntests stderr",
            ),
        ),
        issue_numbers=(41, 42),
    )
    assert [call.prompt.template for call in agent_runner.calls] == [
        PromptTemplate.HOST_CHECK_ISSUE,
        PromptTemplate.HOST_CHECK_ISSUE,
    ]
    assert [call.role for call in agent_runner.calls] == [
        AgentRole.PREFLIGHT_ISSUE,
        AgentRole.PREFLIGHT_ISSUE,
    ]
    assert [call.work_body for call in agent_runner.calls] == [
        "reporting lint host-check issue",
        "reporting tests host-check issue",
    ]
    github_svc.get_issue.assert_called_once_with(42)


def test_run_host_check_run_raises_when_filed_afk_issue_labels_are_missing(tmp_path):
    from pycastle.agents.output_protocol import IssueOutput
    from pycastle.commands import host_check_run as run_mod
    from pycastle.config import Config

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "checked-sha"

    def fake_run_host_check(name: str, command: str, cwd: Path) -> None:
        raise run_mod.HostCheckFailedError(
            name=name,
            command=command,
            output=f"{name} stdout\n{name} stderr",
        )

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    agent_runner = FakeAgentRunner(
        [IssueOutput(number=41, labels=["bug", "behavior-slice", "ready-for-agent"])]
    )
    github_svc = MagicMock()
    github_svc.get_issue.return_value = {"body": "x" * 100, "labels": []}

    with pytest.raises(RuntimeError, match="Host-Check Reporter filed issue #41"):
        asyncio.run(
            run_mod.run_host_check_run(
                host_checks=(("lint", "python -c lint"),),
                git_svc=git_svc,
                repo_root=tmp_path,
                cfg=Config(),
                github_svc=github_svc,
                agent_runner=agent_runner,
                status_display=MagicMock(),
                run_host_check=fake_run_host_check,
                transient_worktree_factory=lambda *args, **kwargs: _TransientWorktree(),
            )
        )


def test_run_host_check_run_raises_when_filed_afk_issue_has_multiple_slice_mode_labels(
    tmp_path,
):
    from pycastle.agents.output_protocol import IssueOutput
    from pycastle.commands import host_check_run as run_mod
    from pycastle.config import Config

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "checked-sha"

    def fake_run_host_check(name: str, command: str, cwd: Path) -> None:
        raise run_mod.HostCheckFailedError(
            name=name,
            command=command,
            output=f"{name} stdout\n{name} stderr",
        )

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    agent_runner = FakeAgentRunner(
        [IssueOutput(number=41, labels=["bug", "behavior-slice", "ready-for-agent"])]
    )
    github_svc = MagicMock()
    github_svc.get_issue.return_value = {
        "body": "x" * 100,
        "labels": ["behavior-slice", "docs-slice"],
    }

    with pytest.raises(RuntimeError, match="Host-Check Reporter filed issue #41"):
        asyncio.run(
            run_mod.run_host_check_run(
                host_checks=(("lint", "python -c lint"),),
                git_svc=git_svc,
                repo_root=tmp_path,
                cfg=Config(),
                github_svc=github_svc,
                agent_runner=agent_runner,
                status_display=MagicMock(),
                run_host_check=fake_run_host_check,
                transient_worktree_factory=lambda *args, **kwargs: _TransientWorktree(),
            )
        )


def test_run_host_check_run_raises_when_filed_afk_issue_body_is_too_short(tmp_path):
    from pycastle.agents.output_protocol import IssueOutput
    from pycastle.commands import host_check_run as run_mod
    from pycastle.config import Config

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "checked-sha"

    def fake_run_host_check(name: str, command: str, cwd: Path) -> None:
        raise run_mod.HostCheckFailedError(
            name=name,
            command=command,
            output=f"{name} stdout\n{name} stderr",
        )

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    agent_runner = FakeAgentRunner(
        [IssueOutput(number=41, labels=["bug", "behavior-slice", "ready-for-agent"])]
    )
    github_svc = MagicMock()
    github_svc.get_issue.return_value = {
        "body": "short",
        "labels": ["bug", "behavior-slice", "ready-for-agent"],
    }

    with pytest.raises(
        RuntimeError,
        match="Host-Check Reporter filed issue #41 whose body is below the minimum length floor",
    ):
        asyncio.run(
            run_mod.run_host_check_run(
                host_checks=(("lint", "python -c lint"),),
                git_svc=git_svc,
                repo_root=tmp_path,
                cfg=Config(),
                github_svc=github_svc,
                agent_runner=agent_runner,
                status_display=MagicMock(),
                run_host_check=fake_run_host_check,
                transient_worktree_factory=lambda *args, **kwargs: _TransientWorktree(),
            )
        )


def test_run_host_check_run_raises_clear_error_when_reporter_output_is_not_issue_output(
    tmp_path,
):
    from pycastle.commands import host_check_run as run_mod
    from pycastle.config import Config

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "checked-sha"

    def fake_run_host_check(name: str, command: str, cwd: Path) -> None:
        raise run_mod.HostCheckFailedError(
            name=name,
            command=command,
            output=f"{name} stdout\n{name} stderr",
        )

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    class _BadAgentRunner:
        async def run(self, request):
            return object()

    with pytest.raises(
        RuntimeError,
        match="Host-Check Reporter returned non-issue output: object",
    ):
        asyncio.run(
            run_mod.run_host_check_run(
                host_checks=(("lint", "python -c lint"),),
                git_svc=git_svc,
                repo_root=tmp_path,
                cfg=Config(),
                github_svc=MagicMock(),
                agent_runner=_BadAgentRunner(),
                status_display=MagicMock(),
                run_host_check=fake_run_host_check,
                transient_worktree_factory=lambda *args, **kwargs: _TransientWorktree(),
            )
        )


def test_run_host_check_run_accepts_hitl_issue_without_fetching_issue_body(tmp_path):
    from pycastle.agents.output_protocol import IssueOutput
    from pycastle.commands import host_check_run as run_mod
    from pycastle.config import Config

    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "checked-sha"

    def fake_run_host_check(name: str, command: str, cwd: Path) -> None:
        raise run_mod.HostCheckFailedError(
            name=name,
            command=command,
            output=f"{name} stdout\n{name} stderr",
        )

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    github_svc = MagicMock()
    github_svc.get_issue.side_effect = AssertionError(
        "HITL host-check issues must not be fetched"
    )

    result = asyncio.run(
        run_mod.run_host_check_run(
            host_checks=(("lint", "python -c lint"),),
            git_svc=git_svc,
            repo_root=tmp_path,
            cfg=Config(),
            github_svc=github_svc,
            agent_runner=FakeAgentRunner(
                [IssueOutput(number=41, labels=["bug", "ready-for-human"])]
            ),
            status_display=MagicMock(),
            run_host_check=fake_run_host_check,
            transient_worktree_factory=lambda *args, **kwargs: _TransientWorktree(),
        )
    )

    assert result == run_mod.HostCheckRunFailed(
        checked_sha="checked-sha",
        failures=(
            run_mod.HostCheckFailure(
                name="lint",
                command="python -c lint",
                output="lint stdout\nlint stderr",
            ),
        ),
        issue_numbers=(41,),
    )
    github_svc.get_issue.assert_not_called()
