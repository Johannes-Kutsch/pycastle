import pytest
from unittest.mock import MagicMock

from pycastle.commands.host_check_run import prepare_host_check_run


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
