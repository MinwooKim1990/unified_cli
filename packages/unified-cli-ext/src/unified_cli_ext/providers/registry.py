"""Metadata-only in-memory registry for explicitly installed adapters."""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Dict, Iterable, Mapping, Optional, Tuple

from ..errors import ConfigurationError
from .contract import AdapterDescriptorV1, ProviderAdapterSpecV1

if TYPE_CHECKING:
    from . import runtime as _runtime_types
else:
    class _RuntimeTypesProxy:
        """Resolve public runtime annotations only when introspected."""

        __slots__ = ()

        def __getattr__(self, name: str):
            if name != "ProviderAdapterV1":
                raise AttributeError(name)
            from . import runtime

            return runtime.ProviderAdapterV1

    _runtime_types = _RuntimeTypesProxy()


class ProviderAdapterRegistryV1:
    """Reject duplicates without resolving, importing, or probing providers."""

    def __init__(self, specs: Iterable[ProviderAdapterSpecV1] = ()) -> None:
        self._adapters: Dict[str, _runtime_types.ProviderAdapterV1] = {}
        if isinstance(specs, (str, bytes)):
            raise ConfigurationError("provider adapter registry input is malformed")
        try:
            for index, spec in enumerate(specs):
                if index >= 256:
                    raise ConfigurationError("provider adapter registry exceeds 256 entries")
                self.register(spec)
        except ConfigurationError:
            raise
        except Exception:
            raise ConfigurationError("provider adapter registry input is malformed") from None

    def register(
        self, spec: ProviderAdapterSpecV1,
    ) -> _runtime_types.ProviderAdapterV1:
        from .runtime import ProviderAdapterV1

        adapter = ProviderAdapterV1(spec)
        if adapter.spec.id in self._adapters:
            raise ConfigurationError("duplicate provider adapter id")
        self._adapters[adapter.spec.id] = adapter
        return adapter

    def get(
        self, provider_id: str,
    ) -> Optional[_runtime_types.ProviderAdapterV1]:
        if type(provider_id) is not str:
            return None
        return self._adapters.get(provider_id)

    def descriptors(self) -> Tuple[AdapterDescriptorV1, ...]:
        return tuple(
            self._adapters[key].descriptor for key in sorted(self._adapters)
        )

    @property
    def adapters(self) -> Mapping[str, _runtime_types.ProviderAdapterV1]:
        return MappingProxyType(dict(self._adapters))


__all__ = ["ProviderAdapterRegistryV1"]
