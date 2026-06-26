import os
import shutil
import subprocess
from pathlib import Path
import sys

import pytest


pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="bash unavailable",
)


_BASH = shutil.which("bash") or "bash"


def _bootstrap_repo_with_bare_remote(
    tmp_path: Path, *, clean_seed: bool = False
) -> Path:
    repo = tmp_path / "repo"
    remote = tmp_path / "origin.git"

    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )

    (repo / "README.md").write_text("# Test\n")
    if clean_seed:
        (repo / "bad_format.py").write_text("def  add(  a , b ):\n    return  a+b\n")
        (repo / "lint_fix.py").write_text("linted = True\n")
    else:
        (repo / "bad_format.py").write_text("def  format_bad(   x ):\n    return  x\n")
        (repo / "lint_fix.py").write_text("x = 1\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "fetch", "origin"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "branch", "--set-upstream-to=origin/main", "main"],
        check=True,
        capture_output=True,
    )

    return repo


def _install_ruff_shim(bin_dir: Path, calls_file: Path) -> None:
    ruff = bin_dir / "ruff"
    ruff.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'CALLS_FILE="{calls_file}"\n'
        'printf "%s\n" "$*" >> "$CALLS_FILE"\n'
        'if [ "$1" = "format" ]; then\n'
        "cat > bad_format.py <<'EOF'\n"
        "def  add(  a , b ):\n"
        "    return  a+b\n"
        "EOF\n"
        'elif [ "$1" = "check" ] && [ "$2" = "--fix" ]; then\n'
        'printf "linted = True\n" > lint_fix.py\n'
        "fi\n"
    )
    ruff.chmod(0o755)


@pytest.fixture()
def ruff_shim(tmp_path):
    calls_file = tmp_path / "ruff_calls.txt"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    _install_ruff_shim(bin_dir, calls_file)

    return bin_dir, calls_file


def _run_script(
    script: Path, repo: Path, env: dict[str, str]
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_BASH, str(script)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_publish_workflow_checkout_uses_default_token_and_no_publish_push_token():
    workflow = (
        Path(__file__).parent.parent / ".github" / "workflows" / "publish.yml"
    ).read_text()
    build_job = workflow.split("\n  build:\n", maxsplit=1)[1].split(
        "\n  publish-testpypi:\n", maxsplit=1
    )[0]

    assert "PUBLISH_PUSH_TOKEN" not in workflow
    assert "token:" not in build_job
    assert workflow.count("CI_AUTOFIX_SSH_KEY") == 1


def test_format_and_check_fix_are_applied_then_pushed_for_branch_context(
    tmp_path,
    ruff_shim,
):
    repo = _bootstrap_repo_with_bare_remote(tmp_path)
    (repo / "bad_format.py").write_text("def  format_bad(   x ):\n    return  x\n")
    (repo / "lint_fix.py").write_text("x=2\n")

    ruff_bin, calls_file = ruff_shim
    script = Path(__file__).parent.parent / ".github" / "scripts" / "ci-autofix.sh"

    env = os.environ.copy()
    env["PATH"] = f"{ruff_bin}:{env['PATH']}"
    env["GITHUB_REF"] = "refs/heads/main"

    result = _run_script(script, repo, env)

    assert result.returncode == 0, result.stderr
    assert "fix-pushed" in result.stdout
    assert "format" in calls_file.read_text()
    assert "check --fix" in calls_file.read_text()

    assert (
        repo / "bad_format.py"
    ).read_text() == "def  add(  a , b ):\n    return  a+b\n"
    assert (repo / "lint_fix.py").read_text() == "linted = True\n"

    local_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    remote = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    remote_head = subprocess.run(
        ["git", "-C", remote, "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert local_head == remote_head


def test_tag_context_force_moves_tag_and_fast_forwards_main(tmp_path, ruff_shim):
    repo = _bootstrap_repo_with_bare_remote(tmp_path)
    subprocess.run(
        ["git", "-C", str(repo), "tag", "v1.2.3", "HEAD"],
        check=True,
        capture_output=True,
    )

    remote = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    (repo / "bad_format.py").write_text("def  format_bad(   x ):\n    return  x\n")
    (repo / "lint_fix.py").write_text("x=2\n")

    ruff_bin, _ = ruff_shim
    script = Path(__file__).parent.parent / ".github" / "scripts" / "ci-autofix.sh"
    env = os.environ.copy()
    env["PATH"] = f"{ruff_bin}:{env['PATH']}"
    env["GITHUB_REF"] = "refs/tags/v1.2.3"

    result = _run_script(script, repo, env)

    assert result.returncode == 0, result.stderr
    assert "fix-pushed" in result.stdout

    local_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    remote_head = subprocess.run(
        ["git", "-C", remote, "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    remote_tag = subprocess.run(
        ["git", "-C", remote, "rev-parse", "refs/tags/v1.2.3"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert local_head == remote_head
    assert local_head == remote_tag


def test_tag_context_rebases_and_force_moves_tag_when_concurrent_push_advances_main(
    tmp_path, ruff_shim
):
    repo = _bootstrap_repo_with_bare_remote(tmp_path)
    subprocess.run(
        ["git", "-C", str(repo), "tag", "v1.2.3", "HEAD"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "push", "origin", "refs/tags/v1.2.3"],
        check=True,
        capture_output=True,
    )

    remote = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    original_remote_tag = subprocess.run(
        ["git", "-C", remote, "rev-parse", "refs/tags/v1.2.3"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # Simulate the concurrent branch-push run landing a commit on main first.
    competitor = tmp_path / "competitor"
    subprocess.run(
        ["git", "clone", remote, str(competitor)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(competitor), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(competitor), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    (competitor / "race.txt").write_text("remote wins race\n")
    subprocess.run(
        ["git", "-C", str(competitor), "add", "race.txt"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(competitor), "commit", "-m", "race"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(competitor), "push", "origin", "HEAD:refs/heads/main"],
        check=True,
        capture_output=True,
    )

    (repo / "bad_format.py").write_text("def  format_bad(   x ):\n    return  x\n")
    (repo / "lint_fix.py").write_text("x=2\n")

    ruff_bin, _ = ruff_shim
    script = Path(__file__).parent.parent / ".github" / "scripts" / "ci-autofix.sh"
    output_file = tmp_path / "github_output.txt"
    env = os.environ.copy()
    env["PATH"] = f"{ruff_bin}:{env['PATH']}"
    env["GITHUB_REF"] = "refs/tags/v1.2.3"
    env["GITHUB_OUTPUT"] = str(output_file)

    result = _run_script(script, repo, env)

    remote_tag = subprocess.run(
        ["git", "-C", remote, "rev-parse", "refs/tags/v1.2.3"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert result.returncode == 0, result.stderr
    assert remote_tag != original_remote_tag, "tag must be force-moved to the rebased commit"
    assert output_file.read_text() == "status=fix-pushed\n"


def test_clean_tree_emits_proceed_and_performs_no_push(tmp_path, ruff_shim):
    repo = _bootstrap_repo_with_bare_remote(tmp_path, clean_seed=True)
    remote = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    local_before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    remote_before = subprocess.run(
        ["git", "-C", remote, "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    ruff_bin, _ = ruff_shim
    script = Path(__file__).parent.parent / ".github" / "scripts" / "ci-autofix.sh"
    env = os.environ.copy()
    env["PATH"] = f"{ruff_bin}:{env['PATH']}"
    env["GITHUB_REF"] = "refs/heads/main"

    result = _run_script(script, repo, env)

    assert result.returncode == 0, result.stderr
    assert "proceed" in result.stdout

    local_after = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    remote_after = subprocess.run(
        ["git", "-C", remote, "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert local_before == local_after == remote_before == remote_after


def test_idempotent_second_run_without_new_changes_emits_proceed(tmp_path, ruff_shim):
    repo = _bootstrap_repo_with_bare_remote(tmp_path)
    (repo / "bad_format.py").write_text("def  format_bad(   x ):\n    return  x\n")
    (repo / "lint_fix.py").write_text("x=2\n")

    ruff_bin, _ = ruff_shim
    script = Path(__file__).parent.parent / ".github" / "scripts" / "ci-autofix.sh"

    env = os.environ.copy()
    env["PATH"] = f"{ruff_bin}:{env['PATH']}"
    env["GITHUB_REF"] = "refs/heads/main"

    first = _run_script(script, repo, env)
    assert first.returncode == 0, first.stderr
    assert "fix-pushed" in first.stdout

    remote = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    local_before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    count_before = int(
        subprocess.run(
            ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )

    second = _run_script(script, repo, env)

    assert second.returncode == 0, second.stderr
    assert "proceed" in second.stdout

    local_after = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    count_after = int(
        subprocess.run(
            ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    remote_head = subprocess.run(
        ["git", "-C", remote, "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert local_before == local_after == remote_head
    assert count_before == count_after


def test_fix_push_writes_status_output_for_workflow_handoff(tmp_path, ruff_shim):
    repo = _bootstrap_repo_with_bare_remote(tmp_path)
    (repo / "bad_format.py").write_text("def  format_bad(   x ):\n    return  x\n")
    (repo / "lint_fix.py").write_text("x=2\n")

    ruff_bin, _ = ruff_shim
    script = Path(__file__).parent.parent / ".github" / "scripts" / "ci-autofix.sh"
    output_file = tmp_path / "github_output.txt"

    env = os.environ.copy()
    env["PATH"] = f"{ruff_bin}:{env['PATH']}"
    env["GITHUB_REF"] = "refs/heads/main"
    env["GITHUB_OUTPUT"] = str(output_file)

    result = _run_script(script, repo, env)

    assert result.returncode == 0, result.stderr
    assert output_file.read_text() == "status=fix-pushed\n"


def test_clean_tree_writes_proceed_status_output(tmp_path, ruff_shim):
    repo = _bootstrap_repo_with_bare_remote(tmp_path, clean_seed=True)

    ruff_bin, _ = ruff_shim
    script = Path(__file__).parent.parent / ".github" / "scripts" / "ci-autofix.sh"
    output_file = tmp_path / "github_output.txt"

    env = os.environ.copy()
    env["PATH"] = f"{ruff_bin}:{env['PATH']}"
    env["GITHUB_REF"] = "refs/heads/main"
    env["GITHUB_OUTPUT"] = str(output_file)

    result = _run_script(script, repo, env)

    assert result.returncode == 0, result.stderr
    assert output_file.read_text() == "status=proceed\n"
