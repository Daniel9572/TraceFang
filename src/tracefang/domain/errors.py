from collections.abc import Sequence


class ProviderError(RuntimeError):
    """Base class for expected data-provider failures."""


class InstrumentNotSupportedError(ProviderError):
    """The provider cannot map or serve the requested instrument."""


class ProviderUnavailableError(ProviderError):
    """The provider is temporarily unavailable."""


class ProviderRateLimitError(ProviderError):
    """The local or upstream provider quota has been exhausted."""


class ProviderDataError(ProviderError):
    """The provider response does not satisfy the adapter contract."""


class ProviderChainExhaustedError(ProviderError):
    def __init__(self, capability: str, failures: Sequence[tuple[str, ProviderError]]) -> None:
        self.capability = capability
        self.failures = tuple(failures)
        details = "; ".join(f"{name}: {error}" for name, error in failures)
        super().__init__(f"all {capability} providers failed ({details})")
