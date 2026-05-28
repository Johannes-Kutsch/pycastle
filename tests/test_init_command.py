import sys
from unittest.mock import patch

import pytest
from click.testing import CliRunner


# ── Issue #784 / #1045: bundled universal Dockerfile contract ─────────────────


def test_universal_dockerfile_template_exists_with_supported_clis():
    """Bundled universal Dockerfile must install the supported agent CLIs."""
    from importlib.resources import files

    pkg = files("pycastle").joinpath("defaults")
    content = (pkg / "Dockerfile").read_text()
    assert "@anthropic-ai/claude-code" in content
    assert "@openai/codex" in content
    assert "opencode-ai" in content


# ── Issue #801 / #1045: gh CLI and stale per-service templates ────────────────


def test_universal_dockerfile_installs_gh_from_github_apt():
    """Bundled universal Dockerfile must install gh via the GitHub apt repository."""
    from importlib.resources import files

    pkg = files("pycastle").joinpath("defaults")
    content = (pkg / "Dockerfile").read_text()
    assert "cli.github.com/packages" in content
    assert "apt-get install" in content and " gh" in content


@pytest.mark.parametrize("service", ["claude", "codex", "opencode"])
def test_service_specific_bundled_dockerfiles_are_not_present(service: str):
    """Service-specific bundled Dockerfiles are stale and must not ship."""
    from importlib.resources import files

    pkg = files("pycastle").joinpath("defaults")
    assert not (pkg / f"Dockerfile.{service}").is_file()


def test_universal_dockerfile_installs_ripgrep():
    """Bundled universal Dockerfile must install ripgrep for workspace search."""
    from importlib.resources import files

    pkg = files("pycastle").joinpath("defaults")
    content = (pkg / "Dockerfile").read_text()
    assert "ripgrep" in content


def test_init_keeps_credential_flow_but_only_manages_scaffold_files(
    tmp_path, monkeypatch
):
    """Local init keeps the wizard for config.py/.env without scaffolding runtime files."""
    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    prompt_calls: list[str] = []

    def capture_prompt(message: str, *args: object, **kwargs: object) -> str:
        prompt_calls.append(message)
        if "agent services" in message.lower():
            return "claude"
        return ""

    with (
        patch("click.prompt", side_effect=capture_prompt),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    pycastle_dir = tmp_path / "pycastle"
    assert any("GitHub token" in prompt for prompt in prompt_calls)
    assert any("Claude OAuth token" in prompt for prompt in prompt_calls)
    assert (pycastle_dir / "config.py.example").exists()
    assert (pycastle_dir / "setup" / "cron.sh").exists()
    assert (pycastle_dir / "setup" / "cron-install.sh").exists()
    assert (pycastle_dir / "setup" / "cron-uninstall.sh").exists()
    assert (pycastle_dir / "config.py").exists()
    assert (pycastle_dir / ".env").exists()
    assert not (pycastle_dir / ".pycastle-session").exists()
    assert not (pycastle_dir / "Dockerfile.claude").exists()
    assert not (pycastle_dir / "Dockerfile.codex").exists()
    assert not (pycastle_dir / "Dockerfile").exists()
    assert not (pycastle_dir / "prompts").exists()


def test_init_both_services_skip_dockerfiles_and_runtime_state(tmp_path, monkeypatch):
    """Selecting both keeps the wizard flow but skips user-owned overrides and session state."""
    from pycastle.commands.init import main
    from pycastle.session.resume import SESSION_DIR_NAME

    fake_home = tmp_path / "fakehome"
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / ".codex" / "auth.json").write_bytes(b"{}")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", side_effect=["both", "", ""]),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    pycastle_dir = tmp_path / "pycastle"
    assert (pycastle_dir / "config.py").exists()
    assert (pycastle_dir / ".env").exists()
    assert not (tmp_path / SESSION_DIR_NAME).exists()
    assert not (pycastle_dir / "Dockerfile.claude").exists()
    assert not (pycastle_dir / "Dockerfile.codex").exists()
    assert not (pycastle_dir / "Dockerfile").exists()
    assert not (pycastle_dir / "prompts").exists()


@pytest.mark.parametrize(
    "service",
    [
        "claude",
        "codex",
        "both",
    ],
)
def test_init_service_selection_creates_one_universal_dockerfile(
    tmp_path, monkeypatch, service
):
    """Service selection must not scaffold any local Dockerfile override."""
    from pycastle.commands.init import main

    fake_home = tmp_path / "fakehome"
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / ".codex" / "auth.json").write_bytes(b"{}")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", side_effect=[service, "", ""]),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    pycastle_dir = tmp_path / "pycastle"
    assert not (pycastle_dir / "Dockerfile.claude").exists()
    assert not (pycastle_dir / "Dockerfile.codex").exists()
    assert (
        sorted(
            path.name
            for path in pycastle_dir.iterdir()
            if path.name.startswith("Dockerfile")
        )
        == []
    )


def test_init_does_not_overwrite_existing_local_dockerfile(tmp_path, monkeypatch):
    """init must leave an existing user-owned pycastle/Dockerfile untouched."""
    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    dockerfile = tmp_path / "pycastle" / "Dockerfile"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text("# user-owned Dockerfile\n")

    with (
        patch("click.prompt", side_effect=["claude", "", ""]),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    assert dockerfile.read_text() == "# user-owned Dockerfile\n"


def test_init_asks_service_selection_as_first_prompt(tmp_path, monkeypatch):
    """init must ask the service-selection prompt before credential prompts."""
    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    prompt_calls: list[str] = []

    def capture_prompt(message: str, *args: object, **kwargs: object) -> str:
        prompt_calls.append(message)
        return str(kwargs.get("default", ""))

    with (
        patch("click.prompt", side_effect=capture_prompt),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    assert prompt_calls, "No prompts were issued"
    assert "agent services" in prompt_calls[0].lower()


# ── Cycle 1: init scaffolds all expected files ───────────────────────────────


def test_init_creates_only_pycastle_managed_scaffold_files(tmp_path, monkeypatch):
    """init must not scaffold prompt overrides or a local Dockerfile override."""
    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main()

    scaffold = tmp_path / "pycastle"
    assert (scaffold / "config.py").exists()
    assert (scaffold / ".env").exists()
    assert (scaffold / "config.py.example").exists()
    assert not (scaffold / "Dockerfile.claude").exists()
    assert not (scaffold / "Dockerfile.codex").exists()
    assert not (scaffold / "Dockerfile").exists()
    assert (scaffold / ".gitignore").exists()
    assert (scaffold / "setup" / "cron.sh").exists()
    assert (scaffold / "setup" / "cron-install.sh").exists()
    assert (scaffold / "setup" / "cron-uninstall.sh").exists()
    assert not (scaffold / "prompts").exists()


def test_init_writes_local_config_example_with_all_supported_settings(
    tmp_path, monkeypatch
):
    """init always overwrites local config.py.example with the discoverable template."""
    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    example = tmp_path / "pycastle" / "config.py.example"
    example.parent.mkdir()
    example.write_text("# stale example\n")

    with (
        patch("click.prompt", side_effect=["claude", "", ""]),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    content = example.read_text()
    assert content != "# stale example\n"
    for section in (
        "Behaviour",
        "Docker",
        "Labels",
        "Logging",
        "Preflight checks",
        "Host checks",
        "Implement checks",
        "Improve",
        "Stage overrides",
    ):
        assert f"--- {section} ---" in content

    for field_name in (
        "max_iterations",
        "max_parallel",
        "worktree_timeout",
        "idle_timeout",
        "auto_push",
        "timeout_retries",
        "diagnose_on_failure",
        "docker_image_name",
        "bug_label",
        "issue_label",
        "hitl_label",
        "enhancement_label",
        "needs_triage_label",
        "needs_info_label",
        "wontfix_label",
        "refactor_slice_label",
        "behavior_slice_label",
        "docs_slice_label",
        "needs_slice_type_label",
        "logs_dir",
        "preflight_checks",
        "host_checks",
        "implement_checks",
        "improve_mode",
        "improve_max",
        "plan_override",
        "implement_override",
        "review_override",
        "merge_override",
        "preflight_issue_override",
        "improve_override",
    ):
        assert f"{field_name} =" in content, field_name

    assert "auto_file_bugs" not in content
    assert "bug_report_repo" not in content
    assert "bug reporter" not in content.lower()
    for removed_key in (
        "pycastle_dir",
        "prompts_dir",
        "worktrees_dir",
        "env_file",
        "dockerfile",
    ):
        assert f"{removed_key} =" not in content

    assert "Claude model shorthands: haiku, sonnet, opus" in content
    assert (
        "Codex model names: gpt-5.5, gpt-5.4, gpt-5.4-mini, "
        "gpt-5.3-codex, gpt-5.3-codex-spark, gpt-5.2"
    ) in content
    assert "Claude effort values: low, medium, high, xhigh, max" in content
    assert "Codex effort values: low, medium, high, xhigh" in content
    assert "Codex effort values: none, minimal" not in content
    assert "default_service" not in content
    assert 'StageOverride(service="codex"' in content
    assert "fallback=StageOverride(" in content
    assert "injected via prompt" in content
    assert "not run directly by pycastle config" in content


def test_init_writes_separate_host_checks_into_config_example(tmp_path, monkeypatch):
    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", side_effect=["claude", "", ""]),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    content = (tmp_path / "pycastle" / "config.py.example").read_text()

    assert "--- Host checks ---" in content
    assert 'host_checks = (\n    ("pytest", "pytest"),\n)' in content
    assert (
        "preflight_checks = (\n"
        '    ("ruff", "ruff check ."),\n'
        '    ("mypy", "mypy ."),\n'
        '    ("pytest", "pytest"),\n'
        ")"
    ) in content


def test_init_config_example_shows_behavioral_and_logging_config_only(
    tmp_path, monkeypatch
):
    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", side_effect=["claude", "", ""]),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    content = (tmp_path / "pycastle" / "config.py.example").read_text()

    assert "# --- Logging ---" in content
    assert 'logs_dir = Path("pycastle/logs")' in content
    assert "In local config, logs_dir is used directly." in content
    assert "In global config, logs_dir is the parent directory" in content

    assert "# --- Docker ---" in content
    assert "Local-only build artifact name used by `pycastle build`." in content
    assert 'docker_image_name = ""' in content

    for removed_key in (
        "pycastle_dir",
        "prompts_dir",
        "worktrees_dir",
        "env_file",
        "dockerfile",
    ):
        assert f"{removed_key} =" not in content


def test_refresh_config_example_documents_logs_dir_as_global_parent_and_local_direct(
    tmp_path, monkeypatch
):
    from pycastle.commands.init import refresh

    monkeypatch.chdir(tmp_path)

    refresh()

    content = (tmp_path / "pycastle" / "config.py.example").read_text()

    assert "# --- Logging ---" in content
    assert "In global config, logs_dir is the parent directory" in content
    assert "In local config, logs_dir is used directly" in content
    assert 'logs_dir = Path("pycastle/logs")' in content

    for removed_key in (
        "pycastle_dir",
        "prompts_dir",
        "worktrees_dir",
        "env_file",
        "dockerfile",
    ):
        assert f"{removed_key} =" not in content


def test_init_writes_config_example_to_existing_pycastle_home(tmp_path, monkeypatch):
    """init also writes config.py.example to pycastle home when that directory exists."""
    from pycastle.commands.init import main

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.py.example").write_text("# stale global example\n")
    monkeypatch.setenv("PYCASTLE_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    with (
        patch("click.prompt", side_effect=["claude", "", ""]),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    local_content = (tmp_path / "pycastle" / "config.py.example").read_text()
    global_content = (home / "config.py.example").read_text()
    assert global_content == local_content
    assert global_content != "# stale global example\n"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX executable bit not meaningful on Windows",
)
def test_init_cron_sh_is_executable(tmp_path, monkeypatch):
    """cron.sh must be executable after init scaffolds it."""
    import stat

    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main()

    cron_sh = tmp_path / "pycastle" / "setup" / "cron.sh"
    mode = cron_sh.stat().st_mode
    assert mode & stat.S_IXUSR, "cron.sh must be user-executable"


# ── Cycle 2: docker_image_name is set to the inferred project name ────────────


def test_init_writes_commented_docker_image_name_hint_from_cwd(tmp_path, monkeypatch):
    """init must write a commented-out docker_image_name hint pre-filled from CWD."""
    from pycastle.commands.init import main

    project_dir = tmp_path / "My Cool Project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main()

    content = (project_dir / "pycastle" / "config.py").read_text()
    assert '# docker_image_name = "my-cool-project"' in content
    # No active assignment line
    for line in content.splitlines():
        stripped = line.lstrip()
        assert not stripped.startswith("docker_image_name"), (
            f"unexpected active assignment: {line!r}"
        )


# ── Cycle 5: scaffolded config.py contains StageOverride import and overrides ──


def test_init_config_contains_stage_override_import_and_defaults(tmp_path, monkeypatch):
    """Scaffolded config.py must import StageOverride and define the default stage chains."""
    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main()

    content = (tmp_path / "pycastle" / "config.py").read_text()
    assert "from pycastle import StageOverride" in content
    assert 'service="codex"' in content
    assert 'model="gpt-5.4-mini"' in content
    assert (
        'fallback=StageOverride(service="claude", model="haiku", effort="low")'
        in content
    )
    assert "implement_override = StageOverride(" in content
    assert "review_override = StageOverride(" in content
    assert "preflight_issue_override = StageOverride(" in content
    assert "improve_override = StageOverride(" in content


def test_init_scaffolds_universal_stage_priority_chains_into_config_files(
    tmp_path, monkeypatch
):
    from pycastle import StageOverride
    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main()

    config_content = (tmp_path / "pycastle" / "config.py").read_text()
    example_content = (tmp_path / "pycastle" / "config.py.example").read_text()

    config_ns: dict[str, object] = {}
    example_ns: dict[str, object] = {}
    exec(config_content, config_ns)
    exec(example_content, example_ns)

    expected = {
        "plan_override": StageOverride(
            service="codex",
            model="gpt-5.4-mini",
            effort="low",
            fallback=StageOverride(service="claude", model="haiku", effort="low"),
        ),
        "implement_override": StageOverride(
            service="codex",
            model="gpt-5.4",
            effort="medium",
            fallback=StageOverride(service="claude", model="sonnet", effort="medium"),
        ),
        "review_override": StageOverride(
            service="claude",
            model="sonnet",
            effort="medium",
            fallback=StageOverride(service="codex", model="gpt-5.4", effort="medium"),
        ),
        "merge_override": StageOverride(
            service="codex",
            model="gpt-5.5",
            effort="medium",
            fallback=StageOverride(service="claude", model="opus", effort="high"),
        ),
        "preflight_issue_override": StageOverride(
            service="codex",
            model="gpt-5.5",
            effort="medium",
            fallback=StageOverride(service="claude", model="opus", effort="high"),
        ),
        "improve_override": StageOverride(
            service="codex",
            model="gpt-5.5",
            effort="high",
            fallback=StageOverride(service="claude", model="opus", effort="high"),
        ),
    }

    for key, value in expected.items():
        assert config_ns[key] == value
        assert example_ns[key] == value


# ── Cycle 6: load_config from scaffolded project returns correct StageOverride values ──


def test_load_config_from_scaffolded_project_has_correct_stage_overrides(
    tmp_path, monkeypatch
):
    """load_config on a freshly scaffolded project must return the expected StageOverride values."""
    from pycastle.config import StageOverride, load_config
    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main()

    cfg = load_config(repo_root=tmp_path)
    assert cfg.plan_override == StageOverride(
        service="codex",
        model="gpt-5.4-mini",
        effort="low",
        fallback=StageOverride(service="claude", model="haiku", effort="low"),
    )
    assert cfg.implement_override == StageOverride(
        service="codex",
        model="gpt-5.4",
        effort="medium",
        fallback=StageOverride(service="claude", model="sonnet", effort="medium"),
    )
    assert cfg.review_override == StageOverride(
        service="claude",
        model="sonnet",
        effort="medium",
        fallback=StageOverride(service="codex", model="gpt-5.4", effort="medium"),
    )
    assert cfg.merge_override == StageOverride(
        service="codex",
        model="gpt-5.5",
        effort="medium",
        fallback=StageOverride(service="claude", model="opus", effort="high"),
    )
    assert cfg.preflight_issue_override == StageOverride(
        service="codex",
        model="gpt-5.5",
        effort="medium",
        fallback=StageOverride(service="claude", model="opus", effort="high"),
    )
    assert cfg.improve_override == StageOverride(
        service="codex",
        model="gpt-5.5",
        effort="high",
        fallback=StageOverride(service="claude", model="opus", effort="high"),
    )


@pytest.mark.parametrize("service", ["claude", "codex", "both"])
def test_init_service_selection_writes_same_stage_chains(
    tmp_path, monkeypatch, service
):
    from pycastle import StageOverride
    from pycastle.config import load_config
    from pycastle.commands.init import main

    workspace = tmp_path / service
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    prompt_values = [service, ""]
    if service != "codex":
        prompt_values.append("")

    with (
        patch("click.prompt", side_effect=prompt_values),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    cfg = load_config(repo_root=workspace)
    assert cfg.plan_override == StageOverride(
        service="codex",
        model="gpt-5.4-mini",
        effort="low",
        fallback=StageOverride(service="claude", model="haiku", effort="low"),
    )
    assert cfg.implement_override == StageOverride(
        service="codex",
        model="gpt-5.4",
        effort="medium",
        fallback=StageOverride(service="claude", model="sonnet", effort="medium"),
    )
    assert cfg.review_override == StageOverride(
        service="claude",
        model="sonnet",
        effort="medium",
        fallback=StageOverride(service="codex", model="gpt-5.4", effort="medium"),
    )
    assert cfg.merge_override == StageOverride(
        service="codex",
        model="gpt-5.5",
        effort="medium",
        fallback=StageOverride(service="claude", model="opus", effort="high"),
    )
    assert cfg.preflight_issue_override == StageOverride(
        service="codex",
        model="gpt-5.5",
        effort="medium",
        fallback=StageOverride(service="claude", model="opus", effort="high"),
    )
    assert cfg.improve_override == StageOverride(
        service="codex",
        model="gpt-5.5",
        effort="high",
        fallback=StageOverride(service="claude", model="opus", effort="high"),
    )


def test_init_service_selection_changes_only_credential_collection(
    tmp_path, monkeypatch
):
    from pycastle.commands.init import main

    def strip_docker_image_hint(content: str) -> str:
        return "\n".join(
            line for line in content.splitlines() if "docker_image_name" not in line
        )

    def run_init(service: str) -> tuple[list[str], str, str]:
        workspace = tmp_path / service
        workspace.mkdir()
        monkeypatch.chdir(workspace)
        prompt_calls: list[str] = []

        def capture_prompt(message: str, *args: object, **kwargs: object) -> str:
            prompt_calls.append(message)
            if "agent services" in message.lower():
                return service
            return ""

        with (
            patch("click.prompt", side_effect=capture_prompt),
            patch("click.confirm", return_value=False),
        ):
            main(scope="local")

        pycastle_dir = workspace / "pycastle"
        return (
            prompt_calls,
            (pycastle_dir / "config.py").read_text(),
            (pycastle_dir / "config.py.example").read_text(),
        )

    claude_prompts, claude_config, claude_example = run_init("claude")
    codex_prompts, codex_config, codex_example = run_init("codex")
    both_prompts, both_config, both_example = run_init("both")

    assert any("GitHub token" in prompt for prompt in claude_prompts)
    assert any("Claude OAuth token" in prompt for prompt in claude_prompts)
    assert any("GitHub token" in prompt for prompt in codex_prompts)
    assert not any("Claude OAuth token" in prompt for prompt in codex_prompts)
    assert any("GitHub token" in prompt for prompt in both_prompts)
    assert any("Claude OAuth token" in prompt for prompt in both_prompts)

    assert (
        strip_docker_image_hint(claude_config)
        == strip_docker_image_hint(codex_config)
        == strip_docker_image_hint(both_config)
    )
    assert claude_example == codex_example == both_example
    for service in ("claude", "codex", "both"):
        pycastle_dir = tmp_path / service / "pycastle"
        assert not (pycastle_dir / "Dockerfile").exists()
        assert not (pycastle_dir / "Dockerfile.claude").exists()
        assert not (pycastle_dir / "Dockerfile.codex").exists()
        assert not (pycastle_dir / "prompts").exists()


def test_init_opencode_selection_adds_env_key_without_changing_stage_policy(
    tmp_path, monkeypatch
):
    from pycastle import StageOverride
    from pycastle.config import load_config
    from pycastle.commands.init import main

    workspace = tmp_path / "opencode"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    prompt_calls: list[str] = []

    def capture_prompt(message: str, *args: object, **kwargs: object) -> str:
        prompt_calls.append(message)
        if "agent services" in message.lower():
            return "opencode"
        return ""

    with (
        patch("click.prompt", side_effect=capture_prompt),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    env_content = (workspace / "pycastle" / ".env").read_text()
    cfg = load_config(repo_root=workspace)

    assert any("GitHub token" in prompt for prompt in prompt_calls)
    assert any("OpenCode Go API key" in prompt for prompt in prompt_calls)
    assert not any("Claude OAuth token" in prompt for prompt in prompt_calls)
    assert "OPENCODE_GO_API_KEY=\n" in env_content
    assert cfg.plan_override == StageOverride(
        service="codex",
        model="gpt-5.4-mini",
        effort="low",
        fallback=StageOverride(service="claude", model="haiku", effort="low"),
    )
    assert cfg.implement_override == StageOverride(
        service="codex",
        model="gpt-5.4",
        effort="medium",
        fallback=StageOverride(service="claude", model="sonnet", effort="medium"),
    )
    assert cfg.review_override == StageOverride(
        service="claude",
        model="sonnet",
        effort="medium",
        fallback=StageOverride(service="codex", model="gpt-5.4", effort="medium"),
    )


@pytest.mark.parametrize("service", ["claude", "codex", "both"])
def test_init_non_opencode_selection_keeps_managed_env_template_unchanged(
    tmp_path, monkeypatch, service
):
    from pycastle.commands.init import main

    workspace = tmp_path / service
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    def capture_prompt(message: str, *args: object, **kwargs: object) -> str:
        if "agent services" in message.lower():
            return service
        return ""

    with (
        patch("click.prompt", side_effect=capture_prompt),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    env_content = (workspace / "pycastle" / ".env").read_text()

    assert "CLAUDE_CODE_OAUTH_TOKEN=\n" in env_content
    assert "GH_TOKEN=\n" in env_content
    assert "OPENCODE_GO_API_KEY=\n" not in env_content


def test_init_opencode_rerun_preserves_existing_credentials_without_overwrite(
    tmp_path, monkeypatch
):
    from pycastle.commands.init import main

    workspace = tmp_path / "opencode-rerun"
    pycastle_dir = workspace / "pycastle"
    pycastle_dir.mkdir(parents=True)
    (pycastle_dir / ".env").write_text(
        "GH_TOKEN=existing-gh\nOPENCODE_GO_API_KEY=existing-opencode\n"
    )
    monkeypatch.chdir(workspace)

    def capture_prompt(message: str, *args: object, **kwargs: object) -> str:
        if "agent services" in message.lower():
            return "opencode"
        return ""

    prompt_mock = patch("click.prompt", side_effect=capture_prompt)
    confirm_mock = patch("click.confirm", return_value=False)
    with prompt_mock as pm, confirm_mock:
        main(scope="local")

    env_content = (pycastle_dir / ".env").read_text()
    prompt_calls = [call.args[0] for call in pm.call_args_list]

    assert "GH_TOKEN=existing-gh" in env_content
    assert "OPENCODE_GO_API_KEY=existing-opencode" in env_content
    assert "CLAUDE_CODE_OAUTH_TOKEN=\n" in env_content
    assert not any("GitHub token" in prompt for prompt in prompt_calls)
    assert not any("OpenCode Go API key" in prompt for prompt in prompt_calls)


def test_init_config_example_documents_opencode_opt_in_stage_chain(
    tmp_path, monkeypatch
):
    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", side_effect=["claude", "", ""]),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    content = (tmp_path / "pycastle" / "config.py.example").read_text()

    assert "OpenCode model ids:" in content
    assert "kimi-k2.6" in content
    assert "OpenCode effort values: medium" in content
    assert (
        'StageOverride(service="opencode", model="kimi-k2.6", effort="medium")'
        in content
    )


# ── Cycle 4: init does not overwrite other existing files ─────────────────────


# ── init scaffolds consolidated standards files ──────────────────────────────


def test_init_does_not_scaffold_consolidated_standards_files(tmp_path, monkeypatch):
    """init must not scaffold prompt override standards files into pycastle/prompts/."""
    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main()

    assert not (tmp_path / "pycastle" / "prompts").exists()


def test_init_does_not_scaffold_coding_standards(tmp_path, monkeypatch):
    """init must not scaffold the deleted CODING_STANDARDS.md."""
    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main()

    assert not (tmp_path / "pycastle" / "prompts").exists()


# ── Cycle 4: init does not overwrite other existing files ─────────────────────


def test_init_does_not_overwrite_existing_non_config_file(tmp_path, monkeypatch):
    """init must not overwrite files other than config.py that already exist."""
    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main()

    env_file = tmp_path / "pycastle" / ".env"
    env_file.write_text("GH_TOKEN=custom_value\n")

    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main()

    assert "custom_value" in env_file.read_text()


# ── Issue #474: --global / --local scope flag and prompt ─────────────────────


def test_init_global_writes_config_and_env_to_pycastle_home(tmp_path, monkeypatch):
    """With --global, config.py and .env are written to PYCASTLE_HOME, not local."""
    from pycastle.commands.init import main

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PYCASTLE_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main(scope="global")

    assert (home / "config.py").exists()
    assert (home / ".env").exists()
    assert not (tmp_path / "pycastle" / "config.py").exists()
    assert not (tmp_path / "pycastle" / ".env").exists()


def test_init_global_keeps_project_shaped_files_local(tmp_path, monkeypatch):
    """With --global, local scaffold still excludes prompt and Dockerfile overrides."""
    from pycastle.commands.init import main

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PYCASTLE_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main(scope="global")

    local = tmp_path / "pycastle"
    assert (local / "config.py.example").exists()
    assert (local / ".gitignore").exists()
    assert not (local / "prompts").exists()
    assert (local / "setup" / "cron.sh").exists()
    assert not (local / "Dockerfile.claude").exists()
    assert not (local / "Dockerfile.codex").exists()
    assert not (local / "Dockerfile").exists()


def test_init_global_skip_existing_config_with_message(tmp_path, monkeypatch, capsys):
    """Existing global config.py is left untouched and a clear message is printed."""
    from pycastle.commands.init import main

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.py").write_text("# preexisting\n")
    monkeypatch.setenv("PYCASTLE_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main(scope="global")

    assert (home / "config.py").read_text() == "# preexisting\n"
    captured = capsys.readouterr().out
    assert "leaving it untouched" in captured
    assert str(home / "config.py") in captured


def test_init_global_merges_missing_keys_into_existing_env(tmp_path, monkeypatch):
    """Existing global .env gets missing template keys merged in; existing values preserved."""
    from pycastle.commands.init import main

    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("GH_TOKEN=preexisting\n")
    monkeypatch.setenv("PYCASTLE_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main(scope="global")

    content = (home / ".env").read_text()
    assert "GH_TOKEN=preexisting" in content
    assert "CLAUDE_CODE_OAUTH_TOKEN=" in content


def test_init_global_skips_credential_prompts_when_present_in_global_env(
    tmp_path, monkeypatch
):
    """With --global and existing credentials, the credential prompts are skipped (overwrite declined by default)."""
    from pycastle.commands.init import main

    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=already-set\nGH_TOKEN=already-set\n"
    )
    monkeypatch.setenv("PYCASTLE_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    prompt_mock = patch("click.prompt", return_value="")
    confirm_mock = patch("click.confirm", return_value=False)
    with prompt_mock as pm, confirm_mock:
        main(scope="global")

    # No credential click.prompt should have been issued (overwrite confirm defaults to False)
    prompt_calls = [c.args[0] for c in pm.call_args_list]
    assert not any("GitHub token" in m for m in prompt_calls)
    assert not any("Claude OAuth token" in m for m in prompt_calls)


def test_init_global_prompts_when_credential_missing_in_global_env(
    tmp_path, monkeypatch
):
    """With --global, missing credentials trigger a prompt and write to global .env."""
    from pycastle.commands.init import main

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PYCASTLE_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    with (
        patch("click.prompt", side_effect=["claude", "new-gh", "new-claude"]),
        patch("click.confirm", return_value=False),
    ):
        main(scope="global")

    env_text = (home / ".env").read_text()
    assert "GH_TOKEN=new-gh" in env_text
    assert "CLAUDE_CODE_OAUTH_TOKEN=new-claude" in env_text


@pytest.mark.parametrize(
    ("delete_local", "local_should_exist"),
    [
        (True, False),
        (False, True),
    ],
)
def test_init_global_asks_whether_to_delete_existing_local_env(
    tmp_path, monkeypatch, delete_local, local_should_exist
):
    """With --global and local .env present, init asks whether to delete the local .env."""
    from pycastle.commands.init import main

    home = tmp_path / "home"
    home.mkdir()
    local_env = tmp_path / "pycastle" / ".env"
    local_env.parent.mkdir()
    local_env.write_text("GH_TOKEN=local-secret\n")
    monkeypatch.setenv("PYCASTLE_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    confirm_messages: list[str] = []

    def confirm_side_effect(message, *args, **kwargs):
        confirm_messages.append(message)
        if "Delete local .env" in message:
            return delete_local
        return False

    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", side_effect=confirm_side_effect),
    ):
        main(scope="global")

    assert any(
        "Delete local .env? (Global will be used instead)" in message
        for message in confirm_messages
    )
    assert local_env.exists() is local_should_exist


def test_init_local_always_prompts_for_credentials(tmp_path, monkeypatch):
    """With --local, credential prompts run even if global .env already has them."""
    from pycastle.commands.init import main

    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(
        "GH_TOKEN=global-set\nCLAUDE_CODE_OAUTH_TOKEN=global-set\n"
    )
    monkeypatch.setenv("PYCASTLE_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    def confirm_side_effect(message, *args, **kwargs):
        if "Create local .env" in message:
            return True
        return False

    with (
        patch("click.prompt", return_value="local-value") as pm,
        patch("click.confirm", side_effect=confirm_side_effect),
    ):
        main(scope="local")

    prompt_calls = [c.args[0] for c in pm.call_args_list]
    assert any("GitHub token" in m for m in prompt_calls)
    assert any("Claude OAuth token" in m for m in prompt_calls)
    local_env = (tmp_path / "pycastle" / ".env").read_text()
    assert "GH_TOKEN=local-value" in local_env
    assert "CLAUDE_CODE_OAUTH_TOKEN=local-value" in local_env


@pytest.mark.parametrize(
    ("create_local", "local_should_exist", "expected_token"),
    [
        (True, True, "local-value"),
        (False, False, None),
    ],
)
def test_init_local_asks_whether_to_create_missing_local_env_when_global_exists(
    tmp_path, monkeypatch, create_local, local_should_exist, expected_token
):
    """With --local, global .env present, and local .env missing, init asks whether to create local .env."""
    from pycastle.commands.init import main

    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(
        "GH_TOKEN=global-secret\nCLAUDE_CODE_OAUTH_TOKEN=global-secret\n"
    )
    monkeypatch.setenv("PYCASTLE_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    confirm_messages: list[str] = []
    prompt_messages: list[str] = []

    def confirm_side_effect(message, *args, **kwargs):
        confirm_messages.append(message)
        if "Create local .env" in message:
            return create_local
        return False

    def prompt_side_effect(message, *args, **kwargs):
        prompt_messages.append(message)
        if "agent services" in message:
            return "claude"
        return "local-value"

    with (
        patch("click.prompt", side_effect=prompt_side_effect),
        patch("click.confirm", side_effect=confirm_side_effect),
    ):
        main(scope="local")

    assert any(
        "Create local .env? (Global stays unchanged, local takes priority)" in message
        for message in confirm_messages
    )

    local_env = tmp_path / "pycastle" / ".env"
    assert local_env.exists() is local_should_exist
    assert (home / ".env").read_text() == (
        "GH_TOKEN=global-secret\nCLAUDE_CODE_OAUTH_TOKEN=global-secret\n"
    )
    if expected_token is None:
        assert not any("GitHub token" in message for message in prompt_messages)
        assert not any("Claude OAuth token" in message for message in prompt_messages)
    else:
        local_text = local_env.read_text()
        assert f"GH_TOKEN={expected_token}" in local_text
        assert f"CLAUDE_CODE_OAUTH_TOKEN={expected_token}" in local_text


def test_init_local_with_global_and_local_env_operates_on_local_without_cross_scope_prompt(
    tmp_path, monkeypatch
):
    """With --local and both .env files present, init operates on local .env only."""
    from pycastle.commands.init import main

    home = tmp_path / "home"
    home.mkdir()
    global_env = home / ".env"
    global_env.write_text("GH_TOKEN=global-secret\n")
    local_env = tmp_path / "pycastle" / ".env"
    local_env.parent.mkdir()
    local_env.write_text("GH_TOKEN=\nCLAUDE_CODE_OAUTH_TOKEN=\n")
    monkeypatch.setenv("PYCASTLE_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    confirm_messages: list[str] = []

    def confirm_side_effect(message, *args, **kwargs):
        confirm_messages.append(message)
        return False

    def prompt_side_effect(message, *args, **kwargs):
        if "agent services" in message:
            return "claude"
        return "local-value"

    with (
        patch("click.prompt", side_effect=prompt_side_effect),
        patch("click.confirm", side_effect=confirm_side_effect),
    ):
        main(scope="local")

    assert not any("Create local .env" in message for message in confirm_messages)
    assert not any("Delete local .env" in message for message in confirm_messages)
    assert global_env.read_text() == "GH_TOKEN=global-secret\n"
    local_text = local_env.read_text()
    assert "GH_TOKEN=local-value" in local_text
    assert "CLAUDE_CODE_OAUTH_TOKEN=local-value" in local_text


def test_init_global_with_no_local_env_operates_on_global_without_cross_scope_prompt(
    tmp_path, monkeypatch
):
    """With --global and no local .env, init operates on global .env without a cross-scope prompt."""
    from pycastle.commands.init import main

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PYCASTLE_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    confirm_messages: list[str] = []

    def confirm_side_effect(message, *args, **kwargs):
        confirm_messages.append(message)
        return False

    def prompt_side_effect(message, *args, **kwargs):
        if "agent services" in message:
            return "claude"
        return "global-value"

    with (
        patch("click.prompt", side_effect=prompt_side_effect),
        patch("click.confirm", side_effect=confirm_side_effect),
    ):
        main(scope="global")

    assert not any("Create local .env" in message for message in confirm_messages)
    assert not any("Delete local .env" in message for message in confirm_messages)
    assert not (tmp_path / "pycastle" / ".env").exists()
    global_text = (home / ".env").read_text()
    assert "GH_TOKEN=global-value" in global_text
    assert "CLAUDE_CODE_OAUTH_TOKEN=global-value" in global_text


def test_init_no_flag_prompts_for_scope(tmp_path, monkeypatch):
    """Without scope arg, init asks the user; confirming yes scaffolds globally."""
    from pycastle.commands.init import main

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PYCASTLE_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    def confirm_side_effect(message, *args, **kwargs):
        if "global" in message.lower():
            return True
        return False

    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", side_effect=confirm_side_effect),
    ):
        main()

    assert (home / "config.py").exists()
    assert not (tmp_path / "pycastle" / "config.py").exists()


def test_init_cli_global_and_local_flags_mutually_exclusive(tmp_path, monkeypatch):
    """`pycastle init --global --local` must error out without scaffolding."""
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--global", "--local"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()
    assert not (tmp_path / "pycastle").exists()


def test_init_cli_local_flag_skips_scope_prompt(tmp_path, monkeypatch):
    """`pycastle init --local` skips the scope prompt and scaffolds locally."""
    from pycastle.main import main as cli

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PYCASTLE_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    # Provide empty input for the credential prompts; if the scope prompt
    # were also asked, click.confirm would default-False but we want to assert
    # no scope prompt is rendered.
    result = runner.invoke(cli, ["init", "--local"], input="\n\nn\n")
    assert result.exit_code == 0, result.output
    assert "global pycastle home" not in result.output.lower()
    assert (tmp_path / "pycastle" / "config.py").exists()


def test_init_cli_global_flag_skips_scope_prompt(tmp_path, monkeypatch):
    """`pycastle init --global` skips the scope prompt and scaffolds globally."""
    from pycastle.main import main as cli

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PYCASTLE_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--global"], input="\n\nn\n")
    assert result.exit_code == 0, result.output
    assert (home / "config.py").exists()
    assert not (tmp_path / "pycastle" / "config.py").exists()


# ── Issue #910: Service-aware credential prompting and .env key merge ─────────


def test_init_codex_service_does_not_prompt_for_claude_token(tmp_path, monkeypatch):
    """Selecting codex should not prompt for CLAUDE_CODE_OAUTH_TOKEN."""
    from pycastle.commands.init import main

    fake_home = tmp_path / "fakehome"
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / ".codex" / "auth.json").write_bytes(b"{}")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.chdir(tmp_path)

    def prompt_side_effect(*args, **kwargs):
        msg = args[0] if args else ""
        return "codex" if "service" in msg.lower() else ""

    with (
        patch("click.prompt", side_effect=prompt_side_effect) as pm,
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    prompt_calls = [c.args[0] for c in pm.call_args_list]
    assert not any("Claude OAuth token" in m for m in prompt_calls)


def test_init_codex_service_does_not_print_claude_token_warning(
    tmp_path, monkeypatch, capsys
):
    """Selecting codex should not print the 'Set CLAUDE_CODE_OAUTH_TOKEN' warning."""
    from pycastle.commands.init import main

    fake_home = tmp_path / "fakehome"
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / ".codex" / "auth.json").write_bytes(b"{}")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.chdir(tmp_path)

    def prompt_side_effect(*args, **kwargs):
        msg = args[0] if args else ""
        return "codex" if "service" in msg.lower() else ""

    with (
        patch("click.prompt", side_effect=prompt_side_effect),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    out = capsys.readouterr().out
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in out


def test_init_merges_missing_template_keys_into_existing_env(tmp_path, monkeypatch):
    """Re-running init adds missing template keys to .env without touching existing values."""
    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / "pycastle" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("GH_TOKEN=preexisting\n")

    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    content = env_file.read_text()
    assert "GH_TOKEN=preexisting" in content
    assert "CLAUDE_CODE_OAUTH_TOKEN=" in content


def test_init_overwrite_no_preserves_existing_gh_token(tmp_path, monkeypatch):
    """Declining overwrite for GH_TOKEN keeps the existing value unchanged."""
    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / "pycastle" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("GH_TOKEN=existing-gh\nCLAUDE_CODE_OAUTH_TOKEN=\n")

    with (
        patch("click.prompt", return_value="new-value"),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    assert "GH_TOKEN=existing-gh" in env_file.read_text()


def test_init_overwrite_yes_replaces_existing_gh_token(tmp_path, monkeypatch):
    """Confirming overwrite for GH_TOKEN prompts for a new value and writes it."""
    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / "pycastle" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("GH_TOKEN=old-gh\nCLAUDE_CODE_OAUTH_TOKEN=\n")

    def confirm_side_effect(message, *args, **kwargs):
        if "Overwrite" in message and "GH_TOKEN" in message:
            return True
        return False

    def prompt_side_effect(*args, **kwargs):
        msg = args[0] if args else ""
        if "GitHub token" in msg:
            return "new-gh"
        return ""

    with (
        patch("click.prompt", side_effect=prompt_side_effect),
        patch("click.confirm", side_effect=confirm_side_effect),
    ):
        main(scope="local")

    assert "GH_TOKEN=new-gh" in env_file.read_text()


def test_init_overwrite_no_preserves_existing_claude_token(tmp_path, monkeypatch):
    """Declining overwrite for CLAUDE_CODE_OAUTH_TOKEN keeps the existing value unchanged."""
    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / "pycastle" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("GH_TOKEN=\nCLAUDE_CODE_OAUTH_TOKEN=existing-claude\n")

    with (
        patch("click.prompt", return_value="new-value"),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    assert "CLAUDE_CODE_OAUTH_TOKEN=existing-claude" in env_file.read_text()


def test_init_overwrite_yes_replaces_existing_claude_token(tmp_path, monkeypatch):
    """Confirming overwrite for CLAUDE_CODE_OAUTH_TOKEN prompts for a new value and writes it."""
    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / "pycastle" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("GH_TOKEN=\nCLAUDE_CODE_OAUTH_TOKEN=old-claude\n")

    def confirm_side_effect(message, *args, **kwargs):
        if "Overwrite" in message and "CLAUDE_CODE_OAUTH_TOKEN" in message:
            return True
        return False

    def prompt_side_effect(*args, **kwargs):
        msg = args[0] if args else ""
        if "Claude OAuth token" in msg:
            return "new-claude"
        return ""

    with (
        patch("click.prompt", side_effect=prompt_side_effect),
        patch("click.confirm", side_effect=confirm_side_effect),
    ):
        main(scope="local")

    assert "CLAUDE_CODE_OAUTH_TOKEN=new-claude" in env_file.read_text()


@pytest.mark.parametrize("service", ["claude", "both"])
def test_init_claude_and_both_service_prompt_for_both_credentials(
    tmp_path, monkeypatch, service
):
    """Selecting claude or both prompts for both CLAUDE_CODE_OAUTH_TOKEN and GH_TOKEN."""
    from pycastle.commands.init import main

    fake_home = tmp_path / "fakehome"
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / ".codex" / "auth.json").write_bytes(b"{}")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.chdir(tmp_path)

    def prompt_side_effect(*args, **kwargs):
        msg = args[0] if args else ""
        return service if "service" in msg.lower() else ""

    with (
        patch("click.prompt", side_effect=prompt_side_effect) as pm,
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    prompt_calls = [c.args[0] for c in pm.call_args_list]
    assert any("GitHub token" in m for m in prompt_calls)
    assert any("Claude OAuth token" in m for m in prompt_calls)


# ── Issue #483: --refresh flag for non-interactive scaffold updates ──────────


def test_init_refresh_overwrites_stale_prompt_file(tmp_path, monkeypatch):
    """`pycastle init --refresh` leaves a user-owned prompt override untouched."""
    from pycastle.commands.init import refresh

    monkeypatch.chdir(tmp_path)
    plan_prompt = tmp_path / "pycastle" / "prompts" / "plan-prompt.md"
    plan_prompt.parent.mkdir(parents=True, exist_ok=True)
    bundled_bytes = b"STALE LOCAL EDIT\n"
    plan_prompt.write_bytes(bundled_bytes)

    refresh()

    assert plan_prompt.read_bytes() == bundled_bytes


def test_init_refresh_copies_cron_sh(tmp_path, monkeypatch):
    """`pycastle init --refresh` copies cron.sh into the consuming project."""
    from pycastle.commands.init import main, refresh

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    cron_sh = tmp_path / "pycastle" / "setup" / "cron.sh"
    cron_sh.unlink()

    refresh()

    assert cron_sh.exists()


def test_init_refresh_copies_cron_install_sh(tmp_path, monkeypatch):
    """`pycastle init --refresh` copies cron-install.sh into the consuming project."""
    from pycastle.commands.init import main, refresh

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    cron_install = tmp_path / "pycastle" / "setup" / "cron-install.sh"
    cron_install.unlink()

    refresh()

    assert cron_install.exists()


def test_init_refresh_copies_cron_uninstall_sh(tmp_path, monkeypatch):
    """`pycastle init --refresh` copies cron-uninstall.sh into the consuming project."""
    from pycastle.commands.init import main, refresh

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    cron_uninstall = tmp_path / "pycastle" / "setup" / "cron-uninstall.sh"
    cron_uninstall.unlink()

    refresh()

    assert cron_uninstall.exists()


@pytest.mark.parametrize(
    "rel_path",
    [
        "setup/cron.sh",
        "setup/cron-install.sh",
        "setup/cron-uninstall.sh",
    ],
)
def test_init_overwrites_stale_setup_scaffold_files_on_rerun(
    tmp_path, monkeypatch, rel_path
):
    """Re-running init refreshes the setup scaffold files it owns."""
    from importlib.resources import files

    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    target = tmp_path / "pycastle" / rel_path
    bundled_bytes = (files("pycastle").joinpath("defaults") / rel_path).read_bytes()
    target.write_text("STALE LOCAL EDIT\n")

    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    assert target.read_bytes() == bundled_bytes


@pytest.mark.parametrize("rel_path", ["prompts/plan-prompt.md", "Dockerfile"])
def test_init_rerun_preserves_user_owned_overrides(tmp_path, monkeypatch, rel_path):
    """Re-running init leaves prompt and Dockerfile overrides byte-for-byte unchanged."""
    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "pycastle" / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    original = b"user-owned override\n"
    target.write_bytes(original)

    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    assert target.read_bytes() == original


def test_init_refresh_leaves_local_config_unchanged(tmp_path, monkeypatch):
    """`pycastle init --refresh` does not touch local config.py."""
    from pycastle.commands.init import main, refresh

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    config_file = tmp_path / "pycastle" / "config.py"
    original_bytes = config_file.read_bytes()

    refresh()

    assert config_file.read_bytes() == original_bytes


def test_init_refresh_leaves_local_env_unchanged(tmp_path, monkeypatch):
    """`pycastle init --refresh` does not touch local .env."""
    from pycastle.commands.init import main, refresh

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    env_file = tmp_path / "pycastle" / ".env"
    env_file.write_text("GH_TOKEN=secret-value\n")

    refresh()

    assert env_file.read_text() == "GH_TOKEN=secret-value\n"


def test_init_refresh_creates_local_scaffold_without_wizard_or_runtime_state(
    tmp_path, monkeypatch
):
    """`pycastle init --refresh` creates scaffold files only when pycastle/ is missing."""
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    with (
        patch("click.prompt") as prompt_mock,
        patch("click.confirm") as confirm_mock,
    ):
        result = runner.invoke(cli, ["init", "--refresh"])

    assert result.exit_code == 0, result.output
    assert prompt_mock.call_count == 0
    assert confirm_mock.call_count == 0

    pycastle_dir = tmp_path / "pycastle"
    assert pycastle_dir.is_dir()
    assert (pycastle_dir / "config.py.example").exists()
    assert (pycastle_dir / "setup" / "cron.sh").exists()
    assert (pycastle_dir / "setup" / "cron-install.sh").exists()
    assert (pycastle_dir / "setup" / "cron-uninstall.sh").exists()
    assert not (pycastle_dir / "prompts").exists()
    assert not (pycastle_dir / "Dockerfile").exists()
    assert not (pycastle_dir / ".pycastle-session").exists()


def test_init_refresh_and_global_mutually_exclusive(tmp_path, monkeypatch):
    """`pycastle init --refresh --global` fails with a usage error."""
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--refresh", "--global"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


def test_init_refresh_and_local_mutually_exclusive(tmp_path, monkeypatch):
    """`pycastle init --refresh --local` fails with a usage error."""
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--refresh", "--local"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


def test_init_refresh_does_not_invoke_credential_or_labels_prompts(
    tmp_path, monkeypatch
):
    """`pycastle init --refresh` skips the credential wizard and labels prompt."""
    from pycastle.commands.init import main, refresh

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    with (
        patch("click.prompt") as pm,
        patch("click.confirm") as cm,
    ):
        refresh()

    assert pm.call_count == 0
    assert cm.call_count == 0


def test_init_refresh_cli_prints_layer_summary(tmp_path, monkeypatch):
    """`pycastle init --refresh` prints the layer summary line at startup."""
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        from pycastle.commands.init import main

        main(scope="local")

    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--refresh"])
    assert result.exit_code == 0, result.output
    assert "config:" in result.output.lower() or "layer" in result.output.lower()


# ── Issue #788: init no longer seeds Codex runtime auth/session state ─────────


def test_init_codex_selection_succeeds_without_host_auth_json(tmp_path, monkeypatch):
    """Selecting codex no longer requires ~/.codex/auth.json during init."""
    from pycastle.commands.init import main
    from pycastle.session.resume import SESSION_DIR_NAME

    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.chdir(tmp_path)

    with (
        patch("click.prompt", side_effect=["codex", "gh-token"]),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    assert "GH_TOKEN=gh-token" in (tmp_path / "pycastle" / ".env").read_text()
    assert not (tmp_path / SESSION_DIR_NAME).exists()


def test_init_codex_selection_creates_no_runtime_state_even_with_host_auth(
    tmp_path, monkeypatch
):
    """Host Codex auth on disk is ignored by init; no runtime state is scaffolded."""
    from pycastle.commands.init import main
    from pycastle.session.resume import SESSION_DIR_NAME

    fake_home = tmp_path / "fakehome"
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / ".codex" / "auth.json").write_bytes(b'{"access_token": "tok"}')
    (fake_home / ".codex" / "config.toml").write_text('[model]\nname = "o3"\n')
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.chdir(tmp_path)

    with (
        patch("click.prompt", side_effect=["codex", ""]),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    assert not (tmp_path / SESSION_DIR_NAME).exists()


def test_init_both_rerun_keeps_existing_env_without_runtime_state(
    tmp_path, monkeypatch
):
    """Re-running init with both preserves .env behavior and still creates no session state."""
    from pycastle.commands.init import main
    from pycastle.session.resume import SESSION_DIR_NAME

    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.chdir(tmp_path)

    with (
        patch("click.prompt", side_effect=["claude", "my-gh-token", ""]),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    env_content = (tmp_path / "pycastle" / ".env").read_text()
    with (
        patch("click.prompt", side_effect=["both", "", ""]),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    assert (tmp_path / "pycastle" / ".env").read_text() == env_content
    assert not (tmp_path / SESSION_DIR_NAME).exists()


def test_init_both_without_host_codex_auth_keeps_written_env(tmp_path, monkeypatch):
    """Selecting both with no host Codex auth keeps the normal .env flow and does not fail."""
    from pycastle.commands.init import main
    from pycastle.session.resume import SESSION_DIR_NAME

    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.chdir(tmp_path)

    with (
        patch("click.prompt", side_effect=["both", "my-gh-token", ""]),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    env_file = tmp_path / "pycastle" / ".env"
    assert env_file.exists()
    assert "GH_TOKEN=my-gh-token" in env_file.read_text()
    assert not (tmp_path / SESSION_DIR_NAME).exists()


# ── Issue #790: --refresh picks Dockerfile template by config walk ─────────────


def test_init_refresh_does_not_scaffold_dockerfiles_for_claude_config(
    tmp_path, monkeypatch
):
    """`pycastle init --refresh` leaves Dockerfile scaffolding out entirely."""
    from pycastle.commands.init import main, refresh

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    dockerfile = tmp_path / "pycastle" / "Dockerfile.claude"
    dockerfile.write_text("FROM scratch\n")

    refresh()

    assert dockerfile.read_text() == "FROM scratch\n"
    assert not (tmp_path / "pycastle" / "Dockerfile").exists()


@pytest.mark.parametrize(
    "config_snippet",
    [
        'from pycastle import StageOverride\nplan_override = StageOverride(service="codex")',
        'from pycastle import StageOverride\nplan_override = StageOverride(fallback=StageOverride(service="codex"))',
    ],
    ids=["stage_service", "fallback_service"],
)
def test_init_refresh_codex_config_does_not_create_dockerfile(
    tmp_path, monkeypatch, config_snippet
):
    """`pycastle init --refresh` does not scaffold Dockerfiles from config references."""
    from pycastle.commands.init import main, refresh

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    # Inject a codex-referencing config
    config_file = tmp_path / "pycastle" / "config.py"
    config_file.write_text(config_snippet + "\n")

    refresh()

    dockerfile = tmp_path / "pycastle" / "Dockerfile.codex"
    assert not dockerfile.exists()
    assert not (tmp_path / "pycastle" / "Dockerfile").exists()


def test_init_refresh_adds_no_dockerfile_when_codex_is_referenced(
    tmp_path, monkeypatch
):
    """Refresh leaves customized Dockerfiles untouched and adds no local override."""
    from pycastle.commands.init import refresh

    monkeypatch.chdir(tmp_path)
    pycastle_dir = tmp_path / "pycastle"
    pycastle_dir.mkdir()
    claude_dockerfile = pycastle_dir / "Dockerfile.claude"
    claude_dockerfile.write_text("# customized claude Dockerfile\n")
    (pycastle_dir / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'implement_override = StageOverride(service="codex")\n'
    )

    refresh()

    assert claude_dockerfile.read_text() == "# customized claude Dockerfile\n"
    assert not (pycastle_dir / "Dockerfile.codex").exists()
    assert not (pycastle_dir / "Dockerfile").exists()


def test_init_refresh_legacy_default_service_codex_creates_no_dockerfiles(
    tmp_path, monkeypatch
):
    """Legacy `default_service` values do not trigger Dockerfile scaffolding."""
    from pycastle.commands.init import main, refresh

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    config_file = tmp_path / "pycastle" / "config.py"
    config_file.write_text('default_service = "codex"\n')

    refresh()

    assert not (tmp_path / "pycastle" / "Dockerfile.claude").exists()
    assert not (tmp_path / "pycastle" / "Dockerfile.codex").exists()
    assert not (tmp_path / "pycastle" / "Dockerfile").exists()


def test_init_refresh_leaves_existing_role_codex_dirs_unmodified(tmp_path, monkeypatch):
    """`pycastle init --refresh` does not manage or mutate pre-existing runtime state."""
    from pycastle.commands.init import refresh
    from pycastle.session.resume import SESSION_DIR_NAME

    monkeypatch.chdir(tmp_path)
    session_file = tmp_path / SESSION_DIR_NAME / "implementer" / "codex" / "auth.json"
    session_file.parent.mkdir(parents=True)
    session_file.write_bytes(b'{"access_token": "tok"}')
    mtime_before = session_file.stat().st_mtime
    content_before = session_file.read_bytes()

    refresh()

    assert session_file.stat().st_mtime == mtime_before
    assert session_file.read_bytes() == content_before


def test_init_refresh_codex_config_without_existing_codex_dirs_does_not_create_them(
    tmp_path, monkeypatch
):
    """`pycastle init --refresh` on a codex config with no per-role codex dirs does not create them."""
    from pycastle.agents.output_protocol import AgentRole
    from pycastle.commands.init import main, refresh
    from pycastle.session.resume import SESSION_DIR_NAME

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    # Inject codex into config without having seeded credentials
    config_file = tmp_path / "pycastle" / "config.py"
    config_file.write_text('default_service = "codex"\n')

    refresh()

    for role in AgentRole:
        namespaces = ["main", "issues"] if role == AgentRole.IMPROVE else [""]
        for namespace in namespaces:
            base = tmp_path / SESSION_DIR_NAME / role.value
            role_state_dir = base / namespace if namespace else base
            assert not (role_state_dir / "codex").exists(), (
                f"codex/ dir should not exist for {role.value}/{namespace}"
            )


# ── Issue #848: per-file status report for pycastle init --refresh ────────────


_REPORT_VERBS = ("created ", "unchanged ", "overwrote ", "preserved ")


def _run_refresh_capture(tmp_path, monkeypatch, capsys) -> list[str]:
    """Run refresh() in tmp_path and return the report lines printed to stdout."""
    from pycastle.commands.init import refresh

    monkeypatch.chdir(tmp_path)
    (tmp_path / "pycastle").mkdir(exist_ok=True)
    refresh()
    return [
        ln
        for ln in capsys.readouterr().out.splitlines()
        if ln.startswith(_REPORT_VERBS)
    ]


def test_refresh_reports_created_for_every_copied_file_when_pycastle_dir_empty(
    tmp_path, monkeypatch, capsys
):
    """When refresh creates scaffold files, it must not claim the directory is up to date."""
    from pycastle.commands.init import refresh

    monkeypatch.chdir(tmp_path)
    (tmp_path / "pycastle").mkdir()
    refresh()
    out = capsys.readouterr().out

    on_disk = sorted(
        str(p.relative_to(tmp_path / "pycastle"))
        for p in (tmp_path / "pycastle").rglob("*")
        if p.is_file()
    )
    assert on_disk  # files were actually created
    assert "up to date" not in out.lower()
    assert (tmp_path / "pycastle" / "config.py.example").exists()
    assert not (tmp_path / "pycastle" / "Dockerfile.claude").exists()
    assert not (tmp_path / "pycastle" / "Dockerfile.codex").exists()
    assert not (tmp_path / "pycastle" / "Dockerfile").exists()


def test_refresh_reports_unchanged_when_file_byte_equal(tmp_path, monkeypatch, capsys):
    """Byte-equal files are not shown in stdout; refresh prints only the confirmation line."""
    from importlib.resources import files
    from pycastle.commands.init import refresh

    monkeypatch.chdir(tmp_path)
    refresh()
    capsys.readouterr()

    rel = "setup/cron.sh"
    target = tmp_path / "pycastle" / rel
    target.write_bytes((files("pycastle").joinpath("defaults") / rel).read_bytes())

    refresh()
    out = capsys.readouterr().out
    assert f"unchanged {rel}" not in out
    assert "up to date" in out.lower()


def test_refresh_updates_config_example_in_existing_pycastle_home(
    tmp_path, monkeypatch
):
    from pycastle.commands.init import refresh

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.py.example").write_text("# stale global example\n")
    monkeypatch.setenv("PYCASTLE_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    refresh()

    local_content = (tmp_path / "pycastle" / "config.py.example").read_text()
    global_content = (home / "config.py.example").read_text()
    assert global_content == local_content
    assert global_content != "# stale global example\n"


def test_refresh_reports_overwrote_and_replaces_content_when_file_differs(
    tmp_path, monkeypatch, capsys
):
    rel = "setup/cron.sh"
    target = tmp_path / "pycastle" / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"STALE CONTENT\n")

    report = _run_refresh_capture(tmp_path, monkeypatch, capsys)
    assert f"overwrote {rel}" in report
    assert target.read_bytes() != b"STALE CONTENT\n"


def test_refresh_preserves_existing_config_py(tmp_path, monkeypatch, capsys):
    """config.py content is untouched by refresh and does not appear in stdout."""
    from pycastle.commands.init import refresh

    config = tmp_path / "pycastle" / "config.py"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("# my config\n")

    monkeypatch.chdir(tmp_path)
    refresh()
    out = capsys.readouterr().out
    assert "config.py" not in out
    assert config.read_text() == "# my config\n"


def test_refresh_preserves_existing_env_file(tmp_path, monkeypatch, capsys):
    """`.env` content is untouched by refresh and does not appear in stdout."""
    from pycastle.commands.init import refresh

    env_file = tmp_path / "pycastle" / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("GH_TOKEN=secret\n")

    monkeypatch.chdir(tmp_path)
    refresh()
    out = capsys.readouterr().out
    assert ".env" not in out
    assert env_file.read_text() == "GH_TOKEN=secret\n"


def test_refresh_omits_config_py_and_env_when_absent(tmp_path, monkeypatch, capsys):
    report = _run_refresh_capture(tmp_path, monkeypatch, capsys)
    assert not any(ln.endswith(" config.py") for ln in report)
    assert not any(ln.endswith(" .env") for ln in report)


def test_refresh_report_lines_sorted_alphabetically_across_verbs(
    tmp_path, monkeypatch, capsys
):
    pycastle_dir = tmp_path / "pycastle"
    pycastle_dir.mkdir()
    (pycastle_dir / "config.py").write_text("# config\n")
    (pycastle_dir / ".env").write_text("GH_TOKEN=x\n")

    report = _run_refresh_capture(tmp_path, monkeypatch, capsys)
    paths = [ln.split(" ", 1)[1] for ln in report]
    assert paths == sorted(paths)


def test_refresh_omits_user_added_files(tmp_path, monkeypatch, capsys):
    pycastle_dir = tmp_path / "pycastle"
    pycastle_dir.mkdir()
    (pycastle_dir / "my-custom-file.md").write_text("user content\n")

    report = _run_refresh_capture(tmp_path, monkeypatch, capsys)
    assert not any("my-custom-file.md" in ln for ln in report)


def test_refresh_omits_runtime_artifact_dirs(tmp_path, monkeypatch, capsys):
    pycastle_dir = tmp_path / "pycastle"
    pycastle_dir.mkdir()
    for artifact_dir in (".worktrees", "logs", ".pycastle-session"):
        d = pycastle_dir / artifact_dir
        d.mkdir()
        (d / "some-file.txt").write_text("artifact\n")

    report = _run_refresh_capture(tmp_path, monkeypatch, capsys)
    for dir_name in (".worktrees", "logs", ".pycastle-session"):
        assert not any(dir_name in ln for ln in report)


# ── Issue #905: quiet --refresh output to overwrote-only ─────────────────────


def _run_refresh_stdout(tmp_path, monkeypatch, capsys) -> str:
    """Run refresh() in tmp_path and return raw stdout."""
    from pycastle.commands.init import refresh

    monkeypatch.chdir(tmp_path)
    (tmp_path / "pycastle").mkdir(exist_ok=True)
    refresh()
    return capsys.readouterr().out


def test_refresh_prints_confirmation_when_nothing_modified(
    tmp_path, monkeypatch, capsys
):
    """When no files differ, refresh prints one confirmation line and no per-file lines."""
    from pycastle.commands.init import refresh

    monkeypatch.chdir(tmp_path)
    pycastle_dir = tmp_path / "pycastle"
    pycastle_dir.mkdir()
    refresh()
    capsys.readouterr()

    refresh()
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert "up to date" in lines[0].lower()
    assert not any(
        ln.startswith(("created ", "unchanged ", "overwrote ", "preserved "))
        for ln in lines
    )


def test_refresh_shows_only_overwrote_file_when_one_mutated(
    tmp_path, monkeypatch, capsys
):
    """After mutating one file, refresh prints only that file's overwrote line."""
    from pycastle.commands.init import refresh

    monkeypatch.chdir(tmp_path)
    pycastle_dir = tmp_path / "pycastle"
    pycastle_dir.mkdir()
    refresh()
    capsys.readouterr()

    rel = "setup/cron.sh"
    (pycastle_dir / rel).parent.mkdir(parents=True, exist_ok=True)
    (pycastle_dir / rel).write_bytes(b"STALE CONTENT\n")

    refresh()
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines == [f"overwrote {rel}"]


def test_refresh_does_not_print_created_files(tmp_path, monkeypatch, capsys):
    """Files copied for the first time (created) do not appear in stdout."""
    out = _run_refresh_stdout(tmp_path, monkeypatch, capsys)
    assert not any(ln.startswith("created ") for ln in out.splitlines())


def test_refresh_does_not_print_config_py_or_env_even_when_present(
    tmp_path, monkeypatch, capsys
):
    """config.py and .env never appear in stdout regardless of their state."""
    from pycastle.commands.init import refresh

    monkeypatch.chdir(tmp_path)
    pycastle_dir = tmp_path / "pycastle"
    pycastle_dir.mkdir()
    (pycastle_dir / "config.py").write_text("# my config\n")
    (pycastle_dir / ".env").write_text("GH_TOKEN=secret\n")
    refresh()
    out = capsys.readouterr().out
    assert "config.py" not in out
    assert ".env" not in out


@pytest.mark.parametrize(
    ("rel_path", "content"),
    [
        ("prompts/plan-prompt.md", b"user prompt override\n"),
        ("Dockerfile", b"FROM scratch\n"),
    ],
    ids=["prompt_override", "dockerfile_override"],
)
def test_refresh_preserves_user_owned_overrides_and_keeps_up_to_date_output(
    tmp_path, monkeypatch, capsys, rel_path, content
):
    """User-owned prompt and Dockerfile overrides stay untouched and invisible to refresh output."""
    from pycastle.commands.init import refresh

    monkeypatch.chdir(tmp_path)
    pycastle_dir = tmp_path / "pycastle"
    pycastle_dir.mkdir()
    refresh()
    capsys.readouterr()

    target = pycastle_dir / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)

    refresh()
    out = capsys.readouterr().out
    assert rel_path not in out
    assert "up to date" in out.lower()
    assert target.read_bytes() == content
