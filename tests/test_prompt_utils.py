from pycastle.prompt_pipeline import load_standards


# ── Cycle 1: load_standards returns correct key→content mapping ───────────────


def test_load_standards_returns_all_seven_keys(tmp_path):
    standards_dir = tmp_path / "coding-standards"
    standards_dir.mkdir()
    (standards_dir / "tests.md").write_text("testing content")
    (standards_dir / "mocking.md").write_text("mocking content")
    (standards_dir / "interfaces.md").write_text("interfaces content")
    (standards_dir / "deep-modules.md").write_text("deep modules content")
    (standards_dir / "refactoring.md").write_text("refactoring content")
    (standards_dir / "language.md").write_text("language content")
    (standards_dir / "deepening.md").write_text("deepening content")

    result = load_standards(tmp_path)

    assert result["TESTING_STANDARDS"] == "testing content"
    assert result["MOCKING_STANDARDS"] == "mocking content"
    assert result["INTERFACES_STANDARDS"] == "interfaces content"
    assert result["DEEP_MODULES_STANDARDS"] == "deep modules content"
    assert result["REFACTORING_STANDARDS"] == "refactoring content"
    assert result["LANGUAGE_STANDARDS"] == "language content"
    assert result["DEEPENING_STANDARDS"] == "deepening content"


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
    assert result["LANGUAGE_STANDARDS"] == ""
    assert result["DEEPENING_STANDARDS"] == ""


# ── Cycle 3: absent coding-standards directory returns empty strings ──────────


def test_load_standards_returns_empty_strings_when_dir_absent(tmp_path):
    result = load_standards(tmp_path)

    assert result == {
        "TESTING_STANDARDS": "",
        "MOCKING_STANDARDS": "",
        "INTERFACES_STANDARDS": "",
        "DEEP_MODULES_STANDARDS": "",
        "REFACTORING_STANDARDS": "",
        "LANGUAGE_STANDARDS": "",
        "DEEPENING_STANDARDS": "",
    }
