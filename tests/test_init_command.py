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
    assert (scaffold / "prompts" / "CODING_STANDARDS.md").exists()


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


# ── Cycle 3: re-running init does not overwrite an existing config.py ─────────


def test_init_does_not_overwrite_existing_config(tmp_path, monkeypatch):
    """A second init must not modify a pycastle/config.py that already exists."""
    from pycastle.init_command import main

    monkeypatch.chdir(tmp_path)
    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main()

    config_file = tmp_path / "pycastle" / "config.py"
    config_file.write_text('docker_image_name = "my-custom-name"\n')

    with (
        patch("click.prompt", return_value=""),
        patch("click.confirm", return_value=False),
    ):
        main()

    assert config_file.read_text() == 'docker_image_name = "my-custom-name"\n'
