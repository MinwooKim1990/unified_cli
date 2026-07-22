"""Stable, non-secret-bearing errors for extension transports."""

from __future__ import annotations


class ExtensionError(Exception):
    """Base class for public extension runtime failures."""


class ConfigurationError(ExtensionError):
    """A caller supplied an unsafe or invalid configuration."""


class ProtocolError(ExtensionError):
    """A peer violated a bounded protocol contract."""


class TransportError(ExtensionError):
    """A transport failed without exposing provider diagnostics."""


class TransportTimeout(TransportError):
    """The configured monotonic deadline expired."""


class TransportCancelled(TransportError):
    """An explicit cancellation request stopped an operation."""


class LimitExceeded(ProtocolError):
    """A line, event, body, or output crossed its configured ceiling."""


class ProcessFailed(TransportError):
    """A subprocess exited unsuccessfully.

    ``diagnostics`` is bounded and redacted before it reaches this object.
    """

    def __init__(self, returncode: int, diagnostics: str = "") -> None:
        self.returncode = returncode
        self.diagnostics = diagnostics
        message = "extension subprocess failed with exit code {}".format(returncode)
        super().__init__(message)


class ProviderReportedError(TransportError):
    """A provider emitted a canonical error event before clean completion."""

    def __init__(self, *, retryable: bool = False) -> None:
        if type(retryable) is not bool:
            retryable = False
        self.retryable = retryable
        super().__init__("provider reported a turn failure")


class OptionalDependencyError(ExtensionError):
    """An explicitly requested optional integration is unavailable."""


class UnsupportedPlatformError(ConfigurationError):
    """The platform cannot provide the promised cleanup semantics."""
