import sys
from unittest.mock import patch

import pytest
from click.testing import CliRunner


# ── Issue #784: service-selection prompt and bundled Dockerfile templates ──────


def test_dockerfile_claude_template_exists_with_claude_cli():
    """Dockerfile.claude must exist in bundled defaults and install Claude Code CLI."""
    from importlib.resources import files

    pkg = files("pycastle").joinpath("defaults")
    content = (pkg / "Dockerfile.claude").read_text()
    assert "claude.ai/install.sh" in content
    assert "npm" not in content


def test_dockerfile_claude_codex_template_exists_with_node_and_codex():
    """Dockerfile.claude-codex must install Claude CLI, Node.js, and @openai/codex."""
    from importlib.resources import files

    pkg = files("pycastle").joinpath("defaults")
    content = (pkg / "Dockerfile.claude-codex").read_text()
    assert "claude.ai/install.sh" in content
    assert "nodejs" in content
    assert "@openai/codex" in content


@pytest.mark.parametrize(
    ("service", "expected_template"),
    [
        ("claude", "Dockerfile.claude"),
        ("codex", "Dockerfile.claude-codex"),
        ("both", "Dockerfile.claude-codex"),
    ],
)
def test_init_service_selection_copies_matching_dockerfile(
    tmp_path, monkeypatch, service, expected_template
):
    """Service answer determines which bundled Dockerfile is copied to pycastle/Dockerfile."""
    from importlib.resources import files

    from pycastle.commands.init import main

    fake_home = tmp_path / "fakehome"
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / ".codex" / "auth.json").write_bytes(b"{}")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", side_effect=[service, "", ""]),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    pkg = files("pycastle").joinpath("defaults")
    expected = (pkg / expected_template).read_bytes()
    actual = (tmp_path / "pycastle" / "Dockerfile").read_bytes()
    assert actual == expected


def test_init_does_not_overwrite_existing_dockerfile(tmp_path, monkeypatch):
    """init must not overwrite an existing pycastle/Dockerfile."""
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


def test_init_creates_all_scaffold_files(tmp_path, monkeypatch):
    """init must copy every template file into pycastle/ without error."""
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
    assert (scaffold / "Dockerfile").exists()
    assert (scaffold / ".gitignore").exists()
    assert (scaffold / "setup" / "cron.sh").exists()
    assert (scaffold / "setup" / "cron-install.sh").exists()
    assert (scaffold / "setup" / "cron-uninstall.sh").exists()
    assert (scaffold / "prompts" / "plan-prompt.md").exists()
    assert (scaffold / "prompts" / "implement" / "behavior.md").exists()
    assert (scaffold / "prompts" / "review-prompt.md").exists()
    assert (scaffold / "prompts" / "merge-prompt.md").exists()


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
    """Scaffolded config.py must import StageOverride and define all four stage overrides."""
    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main()

    content = (tmp_path / "pycastle" / "config.py").read_text()
    assert "from pycastle import StageOverride" in content
    assert 'plan_override = StageOverride(model="haiku", effort="low")' in content
    assert (
        'implement_override = StageOverride(model="sonnet", effort="medium")' in content
    )
    assert 'review_override = StageOverride(model="opus", effort="medium")' in content
    assert 'merge_override = StageOverride(model="opus", effort="high")' in content


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
    assert cfg.plan_override == StageOverride(model="haiku", effort="low")
    assert cfg.implement_override == StageOverride(model="sonnet", effort="medium")
    assert cfg.review_override == StageOverride(model="opus", effort="medium")
    assert cfg.merge_override == StageOverride(model="opus", effort="high")


# ── Cycle 4: init does not overwrite other existing files ─────────────────────


# ── init scaffolds consolidated standards files ──────────────────────────────


def test_init_scaffolds_consolidated_standards_files(tmp_path, monkeypatch):
    """init must copy the consolidated standards files into pycastle/prompts/coding-standards/."""
    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main()

    standards = tmp_path / "pycastle" / "prompts" / "coding-standards"
    assert (standards / "design.md").exists()
    assert (standards / "implementation.md").exists()


def test_init_does_not_scaffold_coding_standards(tmp_path, monkeypatch):
    """init must not scaffold the deleted CODING_STANDARDS.md."""
    from pycastle.commands.init import main

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main()

    assert not (tmp_path / "pycastle" / "prompts" / "CODING_STANDARDS.md").exists()


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
    """With --global, Dockerfile/prompts/.gitignore stay in local pycastle dir."""
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
    assert (local / "Dockerfile").exists()
    assert (local / ".gitignore").exists()
    assert (local / "prompts" / "plan-prompt.md").exists()


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


def test_init_global_skip_existing_env_with_message(tmp_path, monkeypatch, capsys):
    """Existing global .env is left untouched and a clear message is printed."""
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

    assert (home / ".env").read_text() == "GH_TOKEN=preexisting\n"
    captured = capsys.readouterr().out
    assert "leaving it untouched" in captured


def test_init_global_skips_credential_prompts_when_present_in_global_env(
    tmp_path, monkeypatch
):
    """With --global and credentials already in global .env, no prompt is issued."""
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

    # No prompt should have been issued for either credential
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

    with (
        patch("click.prompt", return_value="local-value") as pm,
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    prompt_calls = [c.args[0] for c in pm.call_args_list]
    assert any("GitHub token" in m for m in prompt_calls)
    assert any("Claude OAuth token" in m for m in prompt_calls)
    local_env = (tmp_path / "pycastle" / ".env").read_text()
    assert "GH_TOKEN=local-value" in local_env
    assert "CLAUDE_CODE_OAUTH_TOKEN=local-value" in local_env


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


# ── Issue #483: --refresh flag for non-interactive scaffold updates ──────────


def test_init_refresh_overwrites_stale_prompt_file(tmp_path, monkeypatch):
    """`pycastle init --refresh` rewrites the bundled project-shaped files."""
    from pycastle.commands.init import main, refresh

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    plan_prompt = tmp_path / "pycastle" / "prompts" / "plan-prompt.md"
    bundled_bytes = plan_prompt.read_bytes()
    plan_prompt.write_text("STALE LOCAL EDIT\n")

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


def test_init_refresh_errors_when_no_pycastle_dir(tmp_path, monkeypatch):
    """`pycastle init --refresh` exits non-zero when no local pycastle/ dir exists."""
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--refresh"])
    assert result.exit_code != 0
    assert "pycastle" in result.output.lower()
    assert not (tmp_path / "pycastle").exists()


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


# ── Issue #788: codex credential verification and per-role auth.json seeding ──


def test_init_codex_exits_with_message_when_auth_json_absent(
    tmp_path, monkeypatch, capsys
):
    """init with codex selected prints actionable message and exits non-zero when ~/.codex/auth.json is absent."""
    from pycastle.commands.init import main

    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.chdir(tmp_path)

    with (
        patch("click.prompt", side_effect=["codex", "", ""]),
        patch("click.confirm", return_value=False),
        pytest.raises(SystemExit) as exc_info,
    ):
        main(scope="local")

    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "No codex credentials found at ~/.codex/auth.json" in captured.out
    assert "codex login" in captured.out


def test_init_codex_seeds_auth_json_for_all_roles_when_present(tmp_path, monkeypatch):
    """init with codex seeds byte-identical auth.json into every role × namespace state dir."""
    from pycastle.agents.output_protocol import AgentRole
    from pycastle.commands.init import main
    from pycastle.session.resume import SESSION_DIR_NAME

    fake_home = tmp_path / "fakehome"
    (fake_home / ".codex").mkdir(parents=True)
    host_auth = fake_home / ".codex" / "auth.json"
    host_auth.write_bytes(b'{"access_token": "test-token"}')
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.chdir(tmp_path)

    with (
        patch("click.prompt", side_effect=["codex", "", ""]),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    expected_bytes = host_auth.read_bytes()
    for role in AgentRole:
        namespaces = ["main", "issues"] if role == AgentRole.IMPROVE else [""]
        for namespace in namespaces:
            base = tmp_path / SESSION_DIR_NAME / role.value
            role_state_dir = base / namespace if namespace else base
            auth_file = role_state_dir / "codex" / "auth.json"
            assert auth_file.exists(), f"Missing auth.json for {role.value}/{namespace}"
            assert auth_file.read_bytes() == expected_bytes, (
                f"auth.json content mismatch for {role.value}/{namespace}"
            )


def test_init_codex_seeds_only_auth_json_not_other_files(tmp_path, monkeypatch):
    """init with codex copies only auth.json into role codex dirs, not config.toml or sessions/."""
    from pycastle.agents.output_protocol import AgentRole
    from pycastle.commands.init import main
    from pycastle.session.resume import SESSION_DIR_NAME

    fake_home = tmp_path / "fakehome"
    codex_dir = fake_home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "auth.json").write_bytes(b'{"access_token": "tok"}')
    (codex_dir / "config.toml").write_text('[model]\nname = "o3"\n')
    (codex_dir / "sessions").mkdir()
    (codex_dir / "sessions" / "rollout-abc.jsonl").write_text("{}\n")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.chdir(tmp_path)

    with (
        patch("click.prompt", side_effect=["codex", "", ""]),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    for role in AgentRole:
        namespaces = ["main", "issues"] if role == AgentRole.IMPROVE else [""]
        for namespace in namespaces:
            base = tmp_path / SESSION_DIR_NAME / role.value
            role_state_dir = base / namespace if namespace else base
            codex_state = role_state_dir / "codex"
            assert not (codex_state / "config.toml").exists(), (
                f"config.toml should not be copied for {role.value}/{namespace}"
            )
            assert not (codex_state / "sessions").exists(), (
                f"sessions/ should not be copied for {role.value}/{namespace}"
            )


def test_init_codex_rerun_does_not_overwrite_existing_role_auth_json(
    tmp_path, monkeypatch
):
    """Re-running pycastle init leaves existing role codex/auth.json untouched (mtime and content unchanged)."""
    from pycastle.agents.output_protocol import AgentRole
    from pycastle.commands.init import main
    from pycastle.session.resume import SESSION_DIR_NAME

    fake_home = tmp_path / "fakehome"
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / ".codex" / "auth.json").write_bytes(b'{"access_token": "first"}')
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.chdir(tmp_path)

    with (
        patch("click.prompt", side_effect=["codex", "", ""]),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    # Record mtimes after first init
    impl_auth = (
        tmp_path
        / SESSION_DIR_NAME
        / AgentRole.IMPLEMENTER.value
        / "codex"
        / "auth.json"
    )
    mtime_before = impl_auth.stat().st_mtime

    # Update host auth.json and re-run
    (fake_home / ".codex" / "auth.json").write_bytes(b'{"access_token": "second"}')
    with (
        patch("click.prompt", side_effect=["codex", "", ""]),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    assert impl_auth.stat().st_mtime == mtime_before
    assert impl_auth.read_bytes() == b'{"access_token": "first"}'


def test_init_both_rerun_seeds_codex_without_disturbing_existing_env(
    tmp_path, monkeypatch
):
    """Re-running init with both after claude-only init seeds codex auth without touching existing .env."""
    from pycastle.agents.output_protocol import AgentRole
    from pycastle.commands.init import main
    from pycastle.session.resume import SESSION_DIR_NAME

    fake_home = tmp_path / "fakehome"
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / ".codex" / "auth.json").write_bytes(b'{"access_token": "tok"}')
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.chdir(tmp_path)

    # First init: claude-only
    with (
        patch("click.prompt", side_effect=["claude", "my-gh-token", ""]),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    env_content = (tmp_path / "pycastle" / ".env").read_text()
    assert "GH_TOKEN=my-gh-token" in env_content

    # Second init: both — .env should stay unchanged, codex auth should be seeded
    with (
        patch("click.prompt", side_effect=["both", "", ""]),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    assert (tmp_path / "pycastle" / ".env").read_text() == env_content

    impl_auth = (
        tmp_path
        / SESSION_DIR_NAME
        / AgentRole.IMPLEMENTER.value
        / "codex"
        / "auth.json"
    )
    assert impl_auth.exists()
    assert impl_auth.read_bytes() == b'{"access_token": "tok"}'


def test_init_both_absent_codex_creds_exits_without_rolling_back_env(
    tmp_path, monkeypatch, capsys
):
    """init with both selected and missing codex creds exits non-zero but keeps any .env written by the claude branch."""
    from pycastle.commands.init import main

    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    # No .codex/auth.json
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.chdir(tmp_path)

    with (
        patch("click.prompt", side_effect=["both", "my-gh-token", ""]),
        patch("click.confirm", return_value=False),
        pytest.raises(SystemExit) as exc_info,
    ):
        main(scope="local")

    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "codex login" in captured.out

    env_file = tmp_path / "pycastle" / ".env"
    assert env_file.exists()
    assert "GH_TOKEN=my-gh-token" in env_file.read_text()


def test_init_both_absent_codex_creds_on_rerun_keeps_existing_env(
    tmp_path, monkeypatch, capsys
):
    """Re-running init with both when codex creds missing exits non-zero without rolling back a prior .env write."""
    from pycastle.commands.init import main

    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.chdir(tmp_path)

    # First run: claude-only succeeds, .env written
    with (
        patch("click.prompt", side_effect=["claude", "prior-token", ""]),
        patch("click.confirm", return_value=False),
    ):
        main(scope="local")

    env_file = tmp_path / "pycastle" / ".env"
    assert "GH_TOKEN=prior-token" in env_file.read_text()

    # Second run: both, but no codex creds
    with (
        patch("click.prompt", side_effect=["both", "", ""]),
        patch("click.confirm", return_value=False),
        pytest.raises(SystemExit) as exc_info,
    ):
        main(scope="local")

    assert exc_info.value.code != 0
    assert "GH_TOKEN=prior-token" in env_file.read_text()
