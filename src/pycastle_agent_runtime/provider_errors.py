def __getattr__(name: str):
    if name == "ProviderErrorObservation":
        from pycastle.provider_errors import ProviderErrorObservation

        return ProviderErrorObservation
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["ProviderErrorObservation"]  # noqa: F822
