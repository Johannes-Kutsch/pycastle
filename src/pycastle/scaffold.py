from __future__ import annotations

import stat
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Literal

ScaffoldStatus = Literal["created", "overwrote", "unchanged", "preserved"]

MANAGED_SCAFFOLD_ALLOWLIST = frozenset(
    {
        ".gitignore",
        "setup/cron.sh",
        "setup/cron-install.sh",
        "setup/cron-uninstall.sh",
    }
)


@dataclass(frozen=True)
class ScaffoldArtifactReport:
    status: ScaffoldStatus
    path: str


@dataclass(frozen=True)
class InitScaffold:
    pycastle_dir: Path
    pycastle_home: Path
    defaults: Traversable

    def install_defaults(self, *, config_example_text: str) -> None:
        self.pycastle_dir.mkdir(parents=True, exist_ok=True)
        self._write_config_example(self.pycastle_dir, config_example_text)
        if (self.pycastle_home / "config.py.example").exists():
            self._write_config_example(self.pycastle_home, config_example_text)

        for rel in sorted(MANAGED_SCAFFOLD_ALLOWLIST):
            self._copy_default(rel, self.pycastle_dir / rel)

    def refresh(
        self, *, config_example_text: str
    ) -> tuple[ScaffoldArtifactReport, ...]:
        self.pycastle_dir.mkdir(parents=True, exist_ok=True)

        config_example_path = self.pycastle_dir / "config.py.example"
        report: list[ScaffoldArtifactReport] = [
            ScaffoldArtifactReport(
                status=self._text_status(config_example_path, config_example_text),
                path="config.py.example",
            )
        ]
        self._write_config_example(self.pycastle_dir, config_example_text)
        if (self.pycastle_home / "config.py.example").exists():
            self._write_config_example(self.pycastle_home, config_example_text)

        for rel in sorted(MANAGED_SCAFFOLD_ALLOWLIST):
            target = self.pycastle_dir / rel
            report.append(
                ScaffoldArtifactReport(
                    status=self._default_status(rel, target),
                    path=rel,
                )
            )
            self._copy_default(rel, target)

        for rel in ("config.py", ".env"):
            if (self.pycastle_dir / rel).exists():
                report.append(ScaffoldArtifactReport(status="preserved", path=rel))

        for rel in self._preserved_user_owned_paths():
            report.append(ScaffoldArtifactReport(status="preserved", path=rel))

        return tuple(report)

    def _default_path(self, rel: str) -> Traversable:
        default = self.defaults
        for part in rel.split("/"):
            default = default / part
        return default

    def _copy_default(self, rel: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self._default_path(rel).read_bytes())
        if target.suffix == ".sh":
            target.chmod(
                target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )

    def _write_config_example(self, target_dir: Path, content: str) -> None:
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "config.py.example").write_text(content)

    def _default_status(self, rel: str, target: Path) -> ScaffoldStatus:
        if not target.exists():
            return "created"
        return (
            "unchanged"
            if target.read_bytes() == self._default_path(rel).read_bytes()
            else "overwrote"
        )

    def _preserved_user_owned_paths(self) -> tuple[str, ...]:
        ignored = MANAGED_SCAFFOLD_ALLOWLIST | {
            "config.py.example",
            "config.py",
            ".env",
        }
        return tuple(
            sorted(
                str(path.relative_to(self.pycastle_dir)).replace("\\", "/")
                for path in self.pycastle_dir.rglob("*")
                if path.is_file()
                and str(path.relative_to(self.pycastle_dir)).replace("\\", "/")
                not in ignored
            )
        )

    @staticmethod
    def _text_status(target: Path, expected: str) -> ScaffoldStatus:
        if not target.exists():
            return "created"
        return "unchanged" if target.read_text() == expected else "overwrote"
