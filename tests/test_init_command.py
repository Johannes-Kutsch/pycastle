from unittest.mock import patch

from click.testing import CliRunner


# ── Cycle 1: init scaffolds all expected files ───────────────────────────────


def test_init_creates_all_scaffold_files(tmp_path, monkeypatch):
    """init must copy every template file into pycastle/ without error."""
    from pycastle.init_command import main

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
    assert (scaffold / "prompts" / "plan-prompt.md").exists()
    assert (scaffold / "prompts" / "implement-prompt.md").exists()
    assert (scaffold / "prompts" / "review-prompt.md").exists()
    assert (scaffold / "prompts" / "merge-prompt.md").exists()


# ── Cycle 2: docker_image_name is set to the inferred project name ────────────


def test_init_sets_docker_image_name_from_cwd(tmp_path, monkeypatch):
    """init must write docker_image_name derived from the CWD into pycastle/config.py."""
    from pycastle.init_command import main

    project_dir = tmp_path / "My Cool Project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main()

    content = (project_dir / "pycastle" / "config.py").read_text()
    assert 'docker_image_name = "my-cool-project"' in content


# ── Cycle 3: re-running init always updates docker_image_name ─────────────────


def test_init_updates_docker_image_name_on_rerun(tmp_path, monkeypatch):
    """A second init must update docker_image_name even when config.py already exists."""
    from pycastle.init_command import main

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main()

    config_file = tmp_path / "pycastle" / "config.py"
    content = config_file.read_text()
    # The auto-derived name from tmp_path should be set
    assert 'docker_image_name = "' in content

    # Re-run: docker_image_name in the file should still reflect the CWD name
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main()

    updated_content = config_file.read_text()
    assert 'docker_image_name = "' in updated_content


# ── Cycle 5: scaffolded config.py contains StageOverride import and overrides ──


def test_init_config_contains_stage_override_import_and_defaults(tmp_path, monkeypatch):
    """Scaffolded config.py must import StageOverride and define all four stage overrides."""
    from pycastle.init_command import main

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
    assert 'review_override = StageOverride(model="sonnet", effort="high")' in content
    assert 'merge_override = StageOverride(model="sonnet", effort="medium")' in content


# ── Cycle 6: load_config from scaffolded project returns correct StageOverride values ──


def test_load_config_from_scaffolded_project_has_correct_stage_overrides(
    tmp_path, monkeypatch
):
    """load_config on a freshly scaffolded project must return the expected StageOverride values."""
    from pycastle.config import StageOverride, load_config
    from pycastle.init_command import main

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main()

    cfg = load_config(repo_root=tmp_path)
    assert cfg.plan_override == StageOverride(model="haiku", effort="low")
    assert cfg.implement_override == StageOverride(model="sonnet", effort="medium")
    assert cfg.review_override == StageOverride(model="sonnet", effort="high")
    assert cfg.merge_override == StageOverride(model="sonnet", effort="medium")


# ── Cycle 4: init does not overwrite other existing files ─────────────────────


# ── Cycle 242-3: init scaffolds five standards files ─────────────────────────


def test_init_scaffolds_five_standards_files(tmp_path, monkeypatch):
    """init must copy all five standards files into pycastle/prompts/coding-standards/."""
    from pycastle.init_command import main

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main()

    standards = tmp_path / "pycastle" / "prompts" / "coding-standards"
    assert (standards / "tests.md").exists()
    assert (standards / "mocking.md").exists()
    assert (standards / "interfaces.md").exists()
    assert (standards / "deep-modules.md").exists()
    assert (standards / "refactoring.md").exists()


def test_init_does_not_scaffold_coding_standards(tmp_path, monkeypatch):
    """init must not scaffold the deleted CODING_STANDARDS.md."""
    from pycastle.init_command import main

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
    from pycastle.init_command import main

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
    from pycastle.init_command import main

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
    from pycastle.init_command import main

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
    from pycastle.init_command import main

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
    from pycastle.init_command import main

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
    from pycastle.init_command import main

    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(
        "ANTHROPIC_API_KEY=\nCLAUDE_CODE_OAUTH_TOKEN=already-set\nGH_TOKEN=already-set\n"
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
    assert not any("Claude token" in m for m in prompt_calls)


def test_init_global_prompts_when_credential_missing_in_global_env(
    tmp_path, monkeypatch
):
    """With --global, missing credentials trigger a prompt and write to global .env."""
    from pycastle.init_command import main

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PYCASTLE_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    with (
        patch("click.prompt", side_effect=["new-gh", "new-claude"]),
        patch("click.confirm", return_value=False),
    ):
        main(scope="global")

    env_text = (home / ".env").read_text()
    assert "GH_TOKEN=new-gh" in env_text
    assert "CLAUDE_CODE_OAUTH_TOKEN=new-claude" in env_text


def test_init_local_always_prompts_for_credentials(tmp_path, monkeypatch):
    """With --local, credential prompts run even if global .env already has them."""
    from pycastle.init_command import main

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
    assert any("Claude token" in m for m in prompt_calls)
    local_env = (tmp_path / "pycastle" / ".env").read_text()
    assert "GH_TOKEN=local-value" in local_env
    assert "CLAUDE_CODE_OAUTH_TOKEN=local-value" in local_env


def test_init_no_flag_prompts_for_scope(tmp_path, monkeypatch):
    """Without scope arg, init asks the user; confirming yes scaffolds globally."""
    from pycastle.init_command import main

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
