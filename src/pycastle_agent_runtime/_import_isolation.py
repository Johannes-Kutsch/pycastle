from __future__ import annotations

import sys
from collections.abc import Iterable

_RUNTIME_IMPORT_SNAPSHOT = frozenset(sys.modules)

_FORBIDDEN_PREFIXES = (
    "pycastle.agents",
    "pycastle.infrastructure",
    "pycastle.iteration",
    "pycastle.prompts",
    "pycastle.services",
    "pycastle.session",
)


def assert_runtime_import_isolation(
    *,
    importer: str,
    newly_loaded_modules: Iterable[str] | None = None,
) -> None:
    if newly_loaded_modules is None:
        newly_loaded_modules = frozenset(sys.modules) - _RUNTIME_IMPORT_SNAPSHOT
    imported_application_modules = tuple(
        name
        for name in sorted(set(newly_loaded_modules))
        if any(
            name == prefix or name.startswith(f"{prefix}.")
            for prefix in _FORBIDDEN_PREFIXES
        )
    )
    if not imported_application_modules:
        return
    imported = ", ".join(imported_application_modules)
    raise ImportError(
        f"{importer} imported pycastle application modules during runtime package "
        f"initialization: {imported}. This violates the pycastle_agent_runtime "
        "package boundary."
    )
