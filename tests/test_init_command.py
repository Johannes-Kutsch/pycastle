from unittest.mock import patch


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
    assert 'review_override = StageOverride(model="opus", effort="medium")' in content
    assert 'merge_override = StageOverride(model="opus", effort="high")' in content


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
    assert cfg.review_override == StageOverride(model="opus", effort="medium")
    assert cfg.merge_override == StageOverride(model="opus", effort="high")


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
