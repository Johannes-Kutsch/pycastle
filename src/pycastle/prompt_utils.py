from pathlib import Path

_STANDARDS_FILES = {
    "TESTING_STANDARDS": "tests.md",
    "MOCKING_STANDARDS": "mocking.md",
    "INTERFACES_STANDARDS": "interfaces.md",
    "DEEP_MODULES_STANDARDS": "deep-modules.md",
    "REFACTORING_STANDARDS": "refactoring.md",
}


def load_standards(prompts_dir: Path) -> dict[str, str]:
    standards_dir = prompts_dir / "standards"
    result = {}
    for key, filename in _STANDARDS_FILES.items():
        path = standards_dir / filename
        result[key] = path.read_text(encoding="utf-8") if path.exists() else ""
    return result
