from __future__ import annotations

import dataclasses
import sys


@dataclasses.dataclass(frozen=True)
class _ProviderErrorObservation:
    service_name: str
    raw_provider_text: str
    source_stream: str
    status_code: int | None = None
    provider_code: str | None = None
    error_name: str | None = None


def __getattr__(name: str):
    if name == "ProviderErrorObservation":
        pycastle_provider_errors = sys.modules.get("pycastle.provider_errors")
        if pycastle_provider_errors is not None:
            return getattr(pycastle_provider_errors, name)
        return _ProviderErrorObservation
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["ProviderErrorObservation"]  # noqa: F822
