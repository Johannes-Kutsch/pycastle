from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_DEFAULT_LOCAL_PROMPTS_DIR = Path("pycastle/prompts")
_BUNDLED_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "defaults" / "prompts"


def _is_local_prompts_dir(path: Path) -> bool:
    return (
        path.parts[-len(_DEFAULT_LOCAL_PROMPTS_DIR.parts) :]
        == _DEFAULT_LOCAL_PROMPTS_DIR.parts
    )


@dataclass(frozen=True)
class PromptReference:
    name: str
    relative_path: str


class PromptSource:
    def __init__(
        self,
        local_dir: Path,
        *,
        bundled_dir: Path | None = None,
    ) -> None:
        self._local_dir = local_dir
        self._bundled_dir = bundled_dir
        self._bundled_relative_paths = (
            {
                str(path.relative_to(bundled_dir))
                for path in bundled_dir.rglob("*")
                if path.is_file()
            }
            if bundled_dir is not None and bundled_dir.exists()
            else None
        )

    def unknown_local_relative_paths(self) -> tuple[str, ...]:
        if self._bundled_relative_paths is None or not self._local_dir.exists():
            return ()
        return tuple(
            sorted(
                str(path.relative_to(self._local_dir))
                for path in self._local_dir.rglob("*")
                if (path.is_file() or path.is_symlink())
                and str(path.relative_to(self._local_dir))
                not in self._bundled_relative_paths
            )
        )

    def _resolve_local_override(self, relative_path: str) -> Path | None:
        local_path = self._local_dir / relative_path
        if self._bundled_relative_paths is None:
            if not local_path.is_file():
                return None
            return local_path
        if local_path.is_symlink() or not local_path.is_file():
            return None
        if relative_path in self._bundled_relative_paths:
            return local_path
        return None

    @classmethod
    def for_prompts_dir(cls, prompts_dir: Path) -> PromptSource:
        bundled_dir = (
            _BUNDLED_PROMPTS_DIR if _is_local_prompts_dir(prompts_dir) else None
        )
        return cls(prompts_dir, bundled_dir=bundled_dir)

    def resolve(self, relative_path: str) -> Path:
        local_override = self._resolve_local_override(relative_path)
        if local_override is not None:
            return local_override
        if self._bundled_dir is not None:
            return self._bundled_dir / relative_path
        return self._local_dir / relative_path

    def lookup(self, relative_path: str) -> EffectivePromptFile:
        return EffectivePromptFile(relative_path, self.resolve(relative_path))

    def maybe_lookup(self, relative_path: str) -> EffectivePromptFile | None:
        prompt_file = self.lookup(relative_path)
        if not prompt_file.exists():
            return None
        return prompt_file

    def exists(self, relative_path: str) -> bool:
        return self.lookup(relative_path).exists()

    def read_text(self, relative_path: str) -> str:
        return self.lookup(relative_path).read_text()

    def maybe_read_text(self, relative_path: str) -> str | None:
        prompt_file = self.maybe_lookup(relative_path)
        if prompt_file is None:
            return None
        return prompt_file.read_text()

    def lookup_reference(self, prompt: PromptReference) -> EffectivePromptFile:
        return self.lookup(prompt.relative_path)

    def maybe_lookup_reference(
        self, prompt: PromptReference
    ) -> EffectivePromptFile | None:
        return self.maybe_lookup(prompt.relative_path)


@dataclass(frozen=True)
class EffectivePromptFile:
    relative_path: str
    path: Path

    def exists(self) -> bool:
        return self.path.is_file()

    def read_text(self) -> str:
        return self.path.read_text(encoding="utf-8")
