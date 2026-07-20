"""Lazy boundary for the official Agent Client Protocol Python SDK."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import Any, Callable

from ..errors import OptionalDependencyError, TransportError


_UNSET = object()


def require_acp_sdk() -> ModuleType:
    """Load the official SDK only when ACP support is explicitly requested."""

    if sys.version_info < (3, 10) or sys.version_info >= (3, 15):
        raise OptionalDependencyError(
            "ACP support requires Python >=3.10,<3.15 and unified-cli-ext[acp]"
        )
    try:
        return importlib.import_module("acp")
    except ModuleNotFoundError as exc:
        if exc.name == "acp":
            raise OptionalDependencyError(
                "ACP support requires the optional 'unified-cli-ext[acp]' extra"
            ) from exc
        raise TransportError("ACP SDK import failed") from None
    except Exception:
        raise TransportError("ACP SDK import failed") from None


class AcpSdkAdapter:
    """A deliberately thin lazy wrapper, not an ACP reimplementation.

    The caller supplies a factory against the imported official ``acp`` module.
    This keeps SDK lifecycle and schema semantics owned by the SDK while Ext
    contributes only its lazy optional-dependency boundary.
    """

    def __init__(self, factory: Callable[[ModuleType], Any]) -> None:
        if not callable(factory):
            raise TypeError("ACP adapter factory must be callable")
        self._factory = factory
        self._instance: Any = _UNSET

    @property
    def loaded(self) -> bool:
        return self._instance is not _UNSET

    def open(self) -> Any:
        if self._instance is _UNSET:
            try:
                self._instance = self._factory(require_acp_sdk())
            except OptionalDependencyError:
                raise
            except Exception:
                raise TransportError("ACP SDK factory failed") from None
        return self._instance


__all__ = ["AcpSdkAdapter", "require_acp_sdk"]
