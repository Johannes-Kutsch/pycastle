import asyncio

from pycastle.prompt_pipeline import load_standards, prepare_prompt


def _run(coro):
    return asyncio.run(coro)


async def _noop_exec(cmd: str) -> str:
    return f"output-of:{cmd}"


# ── Cycle 1: load_standards returns correct key→content mapping ───────────────


def test_load_standards_returns_all_five_keys(tmp_path):
    standards_dir = tmp_path / "coding-standards"
    standards_dir.mkdir()
    (standards_dir / "tests.md").write_text("testing content")
    (standards_dir / "mocking.md").write_text("mocking content")
    (standards_dir / "interfaces.md").write_text("interfaces content")
    (standards_dir / "deep-modules.md").write_text("deep modules content")
    (standards_dir / "refactoring.md").write_text("refactoring content")

    result = load_standards(tmp_path)

    assert result["TESTING_STANDARDS"] == "testing content"
    assert result["MOCKING_STANDARDS"] == "mocking content"
    assert result["INTERFACES_STANDARDS"] == "interfaces content"
    assert result["DEEP_MODULES_STANDARDS"] == "deep modules content"
    assert result["REFACTORING_STANDARDS"] == "refactoring content"


# ── Cycle 2: missing standards file returns empty string ─────────────────────


def test_load_standards_returns_empty_string_for_missing_file(tmp_path):
    standards_dir = tmp_path / "coding-standards"
    standards_dir.mkdir()
    (standards_dir / "tests.md").write_text("testing content")
    # other files intentionally absent

    result = load_standards(tmp_path)

    assert result["TESTING_STANDARDS"] == "testing content"
    assert result["MOCKING_STANDARDS"] == ""
    assert result["INTERFACES_STANDARDS"] == ""
    assert result["DEEP_MODULES_STANDARDS"] == ""
    assert result["REFACTORING_STANDARDS"] == ""


# ── Cycle 3: absent coding-standards directory returns empty strings ──────────


def test_load_standards_returns_empty_strings_when_dir_absent(tmp_path):
    result = load_standards(tmp_path)

    assert result == {
        "TESTING_STANDARDS": "",
        "MOCKING_STANDARDS": "",
        "INTERFACES_STANDARDS": "",
        "DEEP_MODULES_STANDARDS": "",
        "REFACTORING_STANDARDS": "",
    }


# ── Cycle 4: prepare_prompt renders standards placeholder ────────────────────


def test_prepare_prompt_renders_standards_placeholder(tmp_path):
    prompt_file = tmp_path / "implement.md"
    prompt_file.write_text("## Testing\n\n{{TESTING_STANDARDS}}\n\nEnd.")

    standards_dir = tmp_path / "coding-standards"
    standards_dir.mkdir()
    (standards_dir / "tests.md").write_text("Good test: assert behavior, not impl.")

    standards = load_standards(tmp_path)
    result = _run(prepare_prompt(prompt_file, standards, _noop_exec))

    assert "Good test: assert behavior, not impl." in result
    assert "{{TESTING_STANDARDS}}" not in result


# ── Cycle 5: shell-shaped arg value is inert (regression for #544) ────────────


def test_arg_value_containing_shell_token_is_not_executed(tmp_path):
    prompt_file = tmp_path / "p.md"
    prompt_file.write_text("Diff:\n{{DIFF}}\n")

    calls = []

    async def recording_exec(cmd: str) -> str:
        calls.append(cmd)
        return "EXECUTED"

    diff_value = "context line\n!`shell`\nmore context"
    result = _run(prepare_prompt(prompt_file, {"DIFF": diff_value}, recording_exec))

    assert "!`shell`" in result
    assert "EXECUTED" not in result
    assert calls == []


# ── Cycle 6: template shell expression coexists with shell-shaped arg value ───


def test_template_shell_expr_runs_arg_shell_token_stays_inert(tmp_path):
    prompt_file = tmp_path / "p.md"
    prompt_file.write_text("Header: !`echo hi`\nBody: {{X}}\n")

    calls = []

    async def recording_exec(cmd: str) -> str:
        calls.append(cmd)
        return "HI"

    result = _run(
        prepare_prompt(prompt_file, {"X": "evil payload !`evil`"}, recording_exec)
    )

    assert calls == ["echo hi"]
    assert "Header: HI" in result
    assert "!`evil`" in result
