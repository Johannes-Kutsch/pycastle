from __future__ import annotations

from pathlib import Path

_DEFAULT_LOCAL_PROMPTS_DIR = Path("pycastle/prompts")
_BUNDLED_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "defaults" / "prompts"


class PromptSource:
    def __init__(
        self,
        local_dir: Path,
        *,
        bundled_dir: Path | None = None,
    ) -> None:
        self._local_dir = local_dir
        self._bundled_dir = bundled_dir

    @classmethod
    def for_prompts_dir(cls, prompts_dir: Path) -> PromptSource:
        bundled_dir = (
            _BUNDLED_PROMPTS_DIR if prompts_dir == _DEFAULT_LOCAL_PROMPTS_DIR else None
        )
        return cls(prompts_dir, bundled_dir=bundled_dir)

    def resolve(self, relative_path: str) -> Path:
        local_path = self._local_dir / relative_path
        if local_path.exists():
            return local_path
        if self._bundled_dir is not None:
            bundled_path = self._bundled_dir / relative_path
            if bundled_path.exists():
                return bundled_path
        return local_path
