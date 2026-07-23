"""Lazy registry for built-in and entry-point provider implementations."""

from __future__ import annotations

import asyncio
import itertools
import math
import os
import threading
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Iterable, Literal, Optional, Tuple, Union

try:  # Python 3.9+ (kept isolated so importing unified_cli never enumerates it)
    from importlib import metadata as importlib_metadata
except ImportError:  # pragma: no cover - defensive for unusual runtimes
    import importlib_metadata  # type: ignore[no-redef]

from .base import BaseProvider, _cancel_requested, _cancelled_error
from .core import ModelInfo, ProviderId, ProviderName
from .errors import UnifiedError
from .plugin import (
    BoundProviderOperationsV1,
    PROVIDER_PLUGIN_ABI_V1,
    ProviderCreateRequestV1,
    ProviderLaunchContextV1,
    ProviderPluginV1,
    ProviderReceiptEnvelopeV1,
    ProviderServerPolicyV1,
    ProviderSupportStatusV1,
    _valid_provider_id,
)

if TYPE_CHECKING:
    from .extension_config import (
        ExtensionLaunchOverridesV1,
        StoredExtensionLaunchV1,
    )


ENTRY_POINT_GROUP = "unified_cli.providers.v1"
DISABLE_PLUGINS_ENV = "UNIFIED_CLI_DISABLE_PLUGINS"
BUILTIN_PROVIDER_IDS: Tuple[ProviderName, ...] = ("claude", "codex", "gemini")
RESERVED_PROVIDER_IDS = frozenset((*BUILTIN_PROVIDER_IDS, "agy"))
# Passive copies of this distribution's entry-point metadata.  Manage-mode
# bootstrap uses only these strings: it must be able to render the complete
# bundled catalog without asking importlib to load provider modules.  A release
# contract test keeps this tuple synchronized with pyproject.toml.
BUNDLED_EXTENSION_ENTRY_POINTS_V1: Tuple[Tuple[str, str], ...] = (
    ("grok", "unified_cli_ext.providers.grok:PLUGIN"),
    ("kimi", "unified_cli_ext.providers.kimi:PLUGIN"),
    ("copilot", "unified_cli_ext.providers.copilot:PLUGIN"),
    ("cursor", "unified_cli_ext.providers.cursor:PLUGIN"),
    ("codebuddy", "unified_cli_ext.providers.codebuddy:PLUGIN"),
    ("qoder", "unified_cli_ext.providers.qoder:PLUGIN"),
    ("mistral-vibe", "unified_cli_ext.providers.mistral_vibe:PLUGIN"),
    ("qwen", "unified_cli_ext.providers.qwen:PLUGIN"),
    ("cline", "unified_cli_ext.providers.cline:PLUGIN"),
    ("opencode", "unified_cli_ext.providers.opencode:PLUGIN"),
    ("kilo", "unified_cli_ext.providers.kilo:PLUGIN"),
    ("droid", "unified_cli_ext.providers.droid:PLUGIN"),
    ("pi", "unified_cli_ext.providers.pi:PLUGIN"),
    ("oh-my-pi", "unified_cli_ext.providers.oh_my_pi:PLUGIN"),
    ("hermes", "unified_cli_ext.providers.hermes:PLUGIN"),
    ("poolside", "unified_cli_ext.providers.poolside:PLUGIN"),
    ("amp", "unified_cli_ext.providers.amp:PLUGIN"),
    ("gitlab-duo", "unified_cli_ext.providers.gitlab_duo:PLUGIN"),
)


@dataclass(frozen=True)
class ProviderDescriptorV1:
    """Safe registry metadata; extension code is never stored in descriptors."""

    id: ProviderId
    source: Literal["builtin", "extension"]
    status: Literal["builtin", "discovered", "loaded", "invalid", "broken"]
    default_model: Optional[str] = None
    capabilities: frozenset[str] = frozenset()
    route_prefixes: Tuple[str, ...] = ()
    server_policy: Optional[ProviderServerPolicyV1] = None
    error: Optional[str] = None
    # Appended so existing positional construction retains its field mapping.
    support_status: Union[
        ProviderSupportStatusV1, Literal["unknown"],
    ] = "unknown"

    @property
    def lifecycle_status(
        self,
    ) -> Literal["builtin", "discovered", "loaded", "invalid", "broken"]:
        """Explicit name for the backwards-compatible ``status`` field."""

        return self.status


# Unversioned convenience name.  The concrete shape remains explicitly v1 so
# a future registry can add another descriptor without mutating this contract.
ProviderDescriptor = ProviderDescriptorV1


@dataclass
class _LoadRecord:
    ready: threading.Event
    plugin: Optional[ProviderPluginV1] = None
    error: Optional[UnifiedError] = None


_LOCK = threading.RLock()
_ENTRY_POINTS: Optional[Tuple[Any, ...]] = None
_DISCOVERY_FAILED = False
_LOADS: Dict[ProviderId, _LoadRecord] = {}
_LOAD_CONTEXT = threading.local()
_CANCELLATION_EXCEPTIONS = (
    KeyboardInterrupt,
    GeneratorExit,
    asyncio.CancelledError,
)
_MAX_EXTENSION_MODELS = 1_000
_MAX_MODEL_ID_CHARS = 512
_MAX_MODEL_DISPLAY_CHARS = 512
_PLUGIN_LOAD_WAIT_SECONDS = 30.0
_MAX_DOCTOR_DEPTH = 8
_MAX_DOCTOR_ITEMS = 512
_MAX_DOCTOR_TEXT_BYTES = 64 * 1024


def _valid_plugin_text(
    value: object,
    *,
    max_chars: int,
    allow_empty: bool,
    require_trimmed: bool,
) -> bool:
    if type(value) is not str:
        return False
    if (not allow_empty and not value) or len(value) > max_chars:
        return False
    if require_trimmed and value != value.strip():
        return False
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return False
    return not any(
        unicodedata.category(char).startswith("C")
        or unicodedata.category(char) in {"Zl", "Zp"}
        for char in value
    )


def plugins_disabled() -> bool:
    return os.environ.get(DISABLE_PLUGINS_ENV, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _plugin_error(provider_id: ProviderId, code: str) -> UnifiedError:
    messages = {
        "disabled": "Provider extensions are disabled in this process.",
        "discovery": f"Provider extension '{provider_id}' is unavailable.",
        "unknown": f"Unknown provider: {provider_id}",
        "invalid_id": "The requested provider id is invalid.",
        "reserved": f"Provider id '{provider_id}' is reserved by unified-cli.",
        "duplicate": f"Provider extension '{provider_id}' is ambiguous.",
        "load": f"Provider extension '{provider_id}' could not be loaded.",
        "abi": f"Provider extension '{provider_id}' uses an unsupported ABI.",
        "metadata": f"Provider extension '{provider_id}' has invalid metadata.",
        "factory": f"Provider extension '{provider_id}' could not be created.",
        "bind": f"Provider extension '{provider_id}' could not bind its launch context.",
        "configure": f"Provider extension '{provider_id}' configuration is invalid.",
        "reentrant": f"Provider extension '{provider_id}' re-entered its own loader.",
        "models": f"Provider extension '{provider_id}' could not list models.",
        "doctor": f"Provider extension '{provider_id}' doctor failed.",
        "held": f"Provider extension '{provider_id}' is held.",
        "runtime": f"Provider extension '{provider_id}' failed while running.",
    }
    hints = {
        "disabled": f"Unset {DISABLE_PLUGINS_ENV} to enable provider extensions.",
        "unknown": "Install a matching provider extension or use claude / codex / gemini.",
        "duplicate": (
            "Remove duplicate provider distributions and retry. If a legacy "
            "local unified-cli-ext wheel was installed, run "
            "'python -m pip uninstall -y unified-cli-ext' and then "
            "'python -m pip install --force-reinstall unified-cli'."
        ),
        "reserved": "Use the built-in public provider id; agy is only an executable alias.",
        "held": "Compatibility is not yet verified; use a supported provider.",
    }
    causes = {
        "discovery": "provider entry-point metadata unavailable",
        "load": "provider entry-point import failed",
        "abi": "provider plugin ABI rejected",
        "metadata": "provider plugin metadata rejected",
        "factory": "provider factory failed",
        "bind": "provider launch binder failed",
        "configure": "provider launch configuration rejected",
        "reentrant": "provider entry-point load was re-entrant",
        "models": "provider model listing failed",
        "doctor": "provider doctor failed",
        "runtime": "extension provider runtime failed",
    }
    return UnifiedError(
        kind="internal" if code == "runtime" else "config",
        provider=provider_id,
        message=messages[code],
        hint=hints.get(code, "Check the provider extension installation."),
        cause=causes.get(code, ""),
    )


def _sanitized_extension_error(
    provider_id: ProviderId, error: UnifiedError,
) -> UnifiedError:
    """Preserve only a trusted error category across the plugin boundary.

    Provider plugins may construct ``UnifiedError`` themselves, so their
    message, hint, cause, provider id, and arbitrary attributes remain
    untrusted.  Reconstructing the error lets bundled adapters report useful
    categories such as ``auth_expired`` without exposing plugin-owned text.
    """

    kind = error.kind if type(error.kind) is str and error.kind in {
        "auth_expired",
        "rate_limit",
        "model_not_allowed",
        "not_found",
        "network",
        "resource_limit",
        "config",
        "internal",
    } else "internal"
    messages = {
        "auth_expired": "Provider authentication is required.",
        "rate_limit": "The provider temporarily refused the request due to a usage limit.",
        "model_not_allowed": "The selected provider model is unavailable for this account.",
        "not_found": "The requested provider resource was not found.",
        "network": "The provider request failed because of a network problem.",
        "resource_limit": "The provider could not complete the request within its resource limits.",
        "config": "The provider configuration is unavailable or incompatible.",
        "internal": f"Provider extension '{provider_id}' failed while running.",
    }
    hints = {
        "auth_expired": (
            "Run the provider's official login command in its unified-cli "
            "provider home, then retry."
        ),
        "rate_limit": "Wait for the provider's stated cooldown before retrying.",
        "model_not_allowed": "Refresh the provider model list or choose another model.",
        "network": "Check the provider service and network connection, then retry.",
        "config": "Run the provider doctor and configuration command, then retry.",
    }
    return UnifiedError(
        kind=kind,
        provider=provider_id,
        message=messages[kind],
        hint=hints.get(kind, ""),
    )


class _ExtensionProviderProxy(BaseProvider):
    """Core-owned error boundary around an explicitly loaded provider."""

    name = "extension"
    default_model = ""
    api_key_env = ""

    def __init__(self, provider_id: ProviderId, inner: BaseProvider):
        self.name = provider_id
        self.default_model = inner.default_model
        self.api_key_env = inner.api_key_env
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @classmethod
    def _discover_bin(cls) -> Optional[str]:  # pragma: no cover - never called
        return None

    @classmethod
    def _install_hint(cls) -> str:  # pragma: no cover - never called
        return ""

    def _build_args(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    def _normalize(self, obj: dict) -> Any:  # pragma: no cover - never called
        raise NotImplementedError

    def _parse_json_response(
        self, text: str, model: str,
    ) -> Any:  # pragma: no cover - never called
        raise NotImplementedError

    def chat(self, prompt: str, **kwargs: Any) -> Any:
        failed = False
        runtime_error: Optional[UnifiedError] = None
        caller_cancelled = _cancel_requested(kwargs.get("cancel_event"))
        if caller_cancelled:
            raise _cancelled_error(self.name) from None
        try:
            response = self._inner.chat(prompt, **kwargs)
        except _CANCELLATION_EXCEPTIONS:
            raise
        except UnifiedError as error:
            caller_cancelled = _cancel_requested(kwargs.get("cancel_event"))
            failed = not caller_cancelled
            runtime_error = error if failed else None
            response = None
        except BaseException:
            caller_cancelled = _cancel_requested(kwargs.get("cancel_event"))
            failed = not caller_cancelled
            response = None
        if caller_cancelled or _cancel_requested(kwargs.get("cancel_event")):
            raise _cancelled_error(self.name) from None
        if runtime_error is not None:
            raise _sanitized_extension_error(self.name, runtime_error) from None
        if failed:
            raise _plugin_error(self.name, "runtime") from None
        return response

    def stream(self, prompt: str, **kwargs: Any) -> Any:
        failed = False
        runtime_error: Optional[UnifiedError] = None
        caller_cancelled = _cancel_requested(kwargs.get("cancel_event"))
        inner_iterator: Any = None
        if caller_cancelled:
            raise _cancelled_error(self.name) from None
        try:
            inner_iterator = iter(self._inner.stream(prompt, **kwargs))
            for message in inner_iterator:
                if _cancel_requested(kwargs.get("cancel_event")):
                    caller_cancelled = True
                    break
                yield message
        except _CANCELLATION_EXCEPTIONS:
            raise
        except UnifiedError as error:
            caller_cancelled = _cancel_requested(kwargs.get("cancel_event"))
            failed = not caller_cancelled
            runtime_error = error if failed else None
        except BaseException:
            caller_cancelled = _cancel_requested(kwargs.get("cancel_event"))
            failed = not caller_cancelled
        finally:
            if inner_iterator is not None:
                try:
                    close = getattr(inner_iterator, "close", None)
                    if callable(close):
                        close()
                except _CANCELLATION_EXCEPTIONS:
                    raise
                except UnifiedError as error:
                    if not caller_cancelled:
                        failed = True
                        runtime_error = error
                except BaseException:
                    if not caller_cancelled:
                        failed = True
        if caller_cancelled or _cancel_requested(kwargs.get("cancel_event")):
            raise _cancelled_error(self.name) from None
        if runtime_error is not None:
            raise _sanitized_extension_error(self.name, runtime_error) from None
        if failed:
            raise _plugin_error(self.name, "runtime") from None

    async def achat(self, prompt: str, **kwargs: Any) -> Any:
        failed = False
        runtime_error: Optional[UnifiedError] = None
        caller_cancelled = _cancel_requested(kwargs.get("cancel_event"))
        if caller_cancelled:
            raise _cancelled_error(self.name) from None
        try:
            response = await self._inner.achat(prompt, **kwargs)
        except _CANCELLATION_EXCEPTIONS:
            raise
        except UnifiedError as error:
            caller_cancelled = _cancel_requested(kwargs.get("cancel_event"))
            failed = not caller_cancelled
            runtime_error = error if failed else None
            response = None
        except BaseException:
            caller_cancelled = _cancel_requested(kwargs.get("cancel_event"))
            failed = not caller_cancelled
            response = None
        if caller_cancelled or _cancel_requested(kwargs.get("cancel_event")):
            raise _cancelled_error(self.name) from None
        if runtime_error is not None:
            raise _sanitized_extension_error(self.name, runtime_error) from None
        if failed:
            raise _plugin_error(self.name, "runtime") from None
        return response

    async def astream(self, prompt: str, **kwargs: Any) -> Any:
        failed = False
        runtime_error: Optional[UnifiedError] = None
        cancelled = False
        caller_cancelled = _cancel_requested(kwargs.get("cancel_event"))
        inner_iterator: Any = None
        if caller_cancelled:
            raise _cancelled_error(self.name) from None
        try:
            stream = self._inner.astream(prompt, **kwargs)
            inner_iterator = stream.__aiter__()
            async for message in inner_iterator:
                if _cancel_requested(kwargs.get("cancel_event")):
                    caller_cancelled = True
                    break
                yield message
        except _CANCELLATION_EXCEPTIONS:
            cancelled = True
            raise
        except UnifiedError as error:
            caller_cancelled = _cancel_requested(kwargs.get("cancel_event"))
            failed = not caller_cancelled
            runtime_error = error if failed else None
        except BaseException:
            caller_cancelled = _cancel_requested(kwargs.get("cancel_event"))
            failed = not caller_cancelled
        finally:
            if inner_iterator is not None:
                try:
                    close = getattr(inner_iterator, "aclose", None)
                    if callable(close):
                        await close()
                except _CANCELLATION_EXCEPTIONS:
                    raise
                except UnifiedError as error:
                    if not cancelled and not caller_cancelled:
                        failed = True
                        runtime_error = error
                except BaseException:
                    if not cancelled and not caller_cancelled:
                        failed = True
        if caller_cancelled or _cancel_requested(kwargs.get("cancel_event")):
            raise _cancelled_error(self.name) from None
        if runtime_error is not None:
            raise _sanitized_extension_error(self.name, runtime_error) from None
        if failed:
            raise _plugin_error(self.name, "runtime") from None


def _entry_points_for_group(raw: Any) -> Iterable[Any]:
    """Normalize importlib.metadata's Python 3.9 and 3.10+ return shapes."""
    select = getattr(raw, "select", None)
    if callable(select):
        return select(group=ENTRY_POINT_GROUP)
    if isinstance(raw, dict):
        return raw.get(ENTRY_POINT_GROUP, ())
    return (
        entry_point for entry_point in raw
        if _entry_point_is_in_group(entry_point)
    )


def _entry_point_is_in_group(entry_point: Any) -> bool:
    try:
        group = getattr(entry_point, "group", None)
    except _CANCELLATION_EXCEPTIONS:
        raise
    except BaseException:
        return False
    return type(group) is str and group == ENTRY_POINT_GROUP


def _discover_entry_points() -> Tuple[Any, ...]:
    global _ENTRY_POINTS, _DISCOVERY_FAILED
    if plugins_disabled():
        return ()
    with _LOCK:
        if _ENTRY_POINTS is not None:
            return _ENTRY_POINTS
        if _DISCOVERY_FAILED:
            raise _plugin_error("extension", "discovery") from None
        discovery_failed = False
        try:
            raw = importlib_metadata.entry_points()
            _ENTRY_POINTS = tuple(_entry_points_for_group(raw))
        except _CANCELLATION_EXCEPTIONS:
            raise
        except BaseException:
            discovery_failed = True
        if discovery_failed:
            # Cache only a boolean. A distribution exception may contain local
            # details that must not remain attached to the public error or its
            # traceback.
            _DISCOVERY_FAILED = True
            raise _plugin_error("extension", "discovery") from None
        return _ENTRY_POINTS


def _safe_entry_point_name(entry_point: Any) -> Optional[str]:
    """Return one canonical metadata name, skipping malformed entries."""
    try:
        name = getattr(entry_point, "name", None)
    except _CANCELLATION_EXCEPTIONS:
        raise
    except BaseException:
        return None
    return name if _valid_provider_id(name) else None


def _matching_entry_points(provider_id: ProviderId) -> Tuple[Any, ...]:
    matches = []
    for entry_point in _discover_entry_points():
        if _safe_entry_point_name(entry_point) == provider_id:
            matches.append(entry_point)
    return tuple(matches)


def _matching_entry_points_safely(provider_id: ProviderId) -> Tuple[Any, ...]:
    failed = False
    try:
        matches = _matching_entry_points(provider_id)
    except _CANCELLATION_EXCEPTIONS:
        raise
    except UnifiedError:
        raise
    except BaseException:
        failed = True
        matches = ()
    if failed:
        raise _plugin_error(provider_id, "discovery") from None
    return matches


def extension_provider_exists(provider_id: ProviderId) -> bool:
    """Metadata-only check used for an explicit ``provider/model`` prefix."""
    if plugins_disabled():
        raise _plugin_error(provider_id, "disabled")
    if not _valid_provider_id(provider_id):
        raise _plugin_error(provider_id, "invalid_id")
    matches = _matching_entry_points_safely(provider_id)
    if provider_id in RESERVED_PROVIDER_IDS and matches:
        raise _plugin_error(provider_id, "reserved")
    if len(matches) > 1:
        raise _plugin_error(provider_id, "duplicate")
    return len(matches) == 1


def _validate_loaded_plugin(
    provider_id: ProviderId, loaded: object,
) -> Tuple[Optional[ProviderPluginV1], Optional[str]]:
    """Evaluate external metadata without raising Core validation errors.

    Every operation in this function may execute extension-controlled code,
    including attribute access, comparisons, iteration, and reconstruction.
    Returning a Core-owned error code lets the caller raise known validation
    decisions only after it has left that metadata evaluation boundary.
    """
    if not isinstance(loaded, ProviderPluginV1):
        if getattr(loaded, "abi_version", PROVIDER_PLUGIN_ABI_V1) != PROVIDER_PLUGIN_ABI_V1:
            return None, "abi"
        return None, "metadata"
    if loaded.abi_version != PROVIDER_PLUGIN_ABI_V1:
        return None, "abi"
    if loaded.id != provider_id:
        return None, "metadata"
    if loaded.id in RESERVED_PROVIDER_IDS:
        return None, "reserved"
    if any(prefix in RESERVED_PROVIDER_IDS for prefix in loaded.route_prefixes):
        return None, "metadata"
    # A frozen plugin can still be corrupted through object.__setattr__ after
    # construction.  Rebuild nested frozen metadata inside this guarded
    # boundary so malformed retained objects become one bounded metadata error.
    server_policy = ProviderServerPolicyV1(
        enabled=loaded.server_policy.enabled,
        requires_external_isolation=(
            loaded.server_policy.requires_external_isolation
        ),
    )
    # Reconstruct the frozen value to re-run validation even if a custom
    # loader created or mutated an instance with object.__setattr__.
    plugin = ProviderPluginV1(
        id=loaded.id,
        factory=loaded.factory,
        default_model=loaded.default_model,
        model_lister=loaded.model_lister,
        doctor=loaded.doctor,
        capabilities=loaded.capabilities,
        route_prefixes=loaded.route_prefixes,
        server_policy=server_policy,
        abi_version=loaded.abi_version,
        support_status=loaded.support_status,
        configuration_abi_version=loaded.configuration_abi_version,
        launch_binder=loaded.launch_binder,
        environment_keys=loaded.environment_keys,
    )
    return plugin, None


def _load_entry_point_safely(provider_id: ProviderId, entry_point: Any) -> object:
    failed = False
    try:
        loaded = entry_point.load()
    except _CANCELLATION_EXCEPTIONS:
        raise
    except BaseException:
        failed = True
        loaded = None
    if failed:
        raise _plugin_error(provider_id, "load") from None
    return loaded


def _validate_loaded_plugin_safely(
    provider_id: ProviderId, loaded: object,
) -> ProviderPluginV1:
    failed = False
    try:
        plugin, validation_error = _validate_loaded_plugin(provider_id, loaded)
    except _CANCELLATION_EXCEPTIONS:
        raise
    except BaseException:
        failed = True
        plugin = None
        validation_error = None
    if failed:
        raise _plugin_error(provider_id, "metadata") from None
    if validation_error is not None:
        raise _plugin_error(provider_id, validation_error) from None
    assert plugin is not None
    return plugin


def load_provider_plugin(provider_id: ProviderId) -> ProviderPluginV1:
    """Load exactly one explicitly requested extension provider, once."""
    if plugins_disabled():
        raise _plugin_error(provider_id, "disabled")
    if not _valid_provider_id(provider_id):
        raise _plugin_error(provider_id, "invalid_id")
    if provider_id in RESERVED_PROVIDER_IDS:
        # Built-ins are resolved by factory.create before reaching this API;
        # this loader is intentionally extensions-only.
        raise _plugin_error(provider_id, "reserved")
    if getattr(_LOAD_CONTEXT, "active", False):
        # Entry-point initializers must not recursively load providers. This
        # also breaks cross-provider wait cycles when two imports start on
        # different threads and then request one another.
        raise _plugin_error(provider_id, "reentrant") from None

    with _LOCK:
        record = _LOADS.get(provider_id)
        if record is None:
            record = _LoadRecord(ready=threading.Event())
            _LOADS[provider_id] = record
            owns_load = True
        else:
            owns_load = False

    if not owns_load:
        if not record.ready.wait(timeout=_PLUGIN_LOAD_WAIT_SECONDS):
            raise _plugin_error(provider_id, "load") from None
        if record.plugin is not None:
            return record.plugin
        assert record.error is not None
        raise record.error

    _LOAD_CONTEXT.active = True
    try:
        unexpected_failure = False
        try:
            matches = _matching_entry_points_safely(provider_id)
            if not matches:
                raise _plugin_error(provider_id, "unknown")
            if len(matches) > 1:
                raise _plugin_error(provider_id, "duplicate")
            loaded = _load_entry_point_safely(provider_id, matches[0])
            plugin = _validate_loaded_plugin_safely(provider_id, loaded)
        except _CANCELLATION_EXCEPTIONS:
            # The initiating caller may cancel, but concurrent waiters still
            # need a deterministic terminal record rather than a permanently
            # unset Event.
            record.error = _plugin_error(provider_id, "load")
            record.ready.set()
            raise
        except UnifiedError as exc:
            record.error = exc
            record.ready.set()
            raise
        except BaseException:
            unexpected_failure = True

        if unexpected_failure:
            error = _plugin_error(provider_id, "load")
            record.error = error
            record.ready.set()
            raise error from None

        record.plugin = plugin
        record.ready.set()
        return plugin
    finally:
        _LOAD_CONTEXT.active = False


def _require_executable_support(plugin: ProviderPluginV1) -> None:
    """Reject Held integrations before any plugin-owned callback can run."""

    if plugin.support_status == "held":
        raise _plugin_error(plugin.id, "held") from None


def _launch_context_for_plugin(
    plugin: ProviderPluginV1,
    extension_launch: Optional["ExtensionLaunchOverridesV1"],
) -> ProviderLaunchContextV1:
    """Merge one explicit Core override over stored receipt/home metadata."""

    from .extension_config import (
        ExtensionLaunchOverridesV1,
        default_provider_home,
        load_extension_launch,
    )

    if extension_launch is not None and type(extension_launch) is not ExtensionLaunchOverridesV1:
        raise _plugin_error(plugin.id, "configure") from None
    launch = extension_launch
    if launch is not None and (
        frozenset(launch.extra_env).difference(plugin.environment_keys)
    ):
        # Typed launch input is an explicit contract. Reject misspelled or
        # undeclared keys before consulting settings or touching provider-home
        # state instead of silently weakening the caller's configuration.
        raise _plugin_error(plugin.id, "configure") from None
    try:
        explicit_source = launch is not None and (
            launch.receipt is not None or launch.bin_path is not None
        )
        stored_receipt = None
        stored_home = None
        if explicit_source:
            # An explicit source supersedes a stale receipt blob, but inherits
            # the separately typed home pointer when the caller did not supply
            # one. Reading this pointer performs no extension discovery.
            from .settings import get_extension_launch_settings

            pointer = get_extension_launch_settings(plugin.id)
            if pointer is not None:
                stored_home = pointer.provider_home
        else:
            stored = load_extension_launch(plugin.id)
            if stored is not None:
                stored_receipt = stored.receipt
                stored_home = stored.provider_home

        receipt = (
            launch.receipt
            if launch is not None and launch.receipt is not None
            else stored_receipt
        )
        bin_path = launch.bin_path if launch is not None else None
        provider_home = (
            launch.provider_home
            if launch is not None and launch.provider_home is not None
            else stored_home
        )
        if provider_home is None:
            provider_home = default_provider_home(plugin.id)
        supplied_env = launch.extra_env if launch is not None else {}
        selected_env = {
            key: supplied_env[key]
            for key in sorted(plugin.environment_keys)
            if key in supplied_env
        }
        return ProviderLaunchContextV1(
            provider_id=plugin.id,
            receipt=receipt,
            bin_path=bin_path,
            provider_home=provider_home,
            provider_env=selected_env,
        )
    except _CANCELLATION_EXCEPTIONS:
        raise
    except UnifiedError:
        raise
    except BaseException:
        raise _plugin_error(plugin.id, "configure") from None


def _bind_plugin_operations(
    plugin: ProviderPluginV1,
    context: ProviderLaunchContextV1,
) -> BoundProviderOperationsV1:
    """Run a plugin binder and reconstruct its result inside one boundary."""

    failed = False
    try:
        binder = plugin.launch_binder
        if not callable(binder):
            raise TypeError("provider has no launch binder")
        supplied = binder(context)
        if type(supplied) is not BoundProviderOperationsV1:
            raise TypeError("provider binder returned incompatible operations")
        normalized_receipt = supplied.normalized_receipt
        if normalized_receipt is not None:
            if type(normalized_receipt) is not ProviderReceiptEnvelopeV1:
                raise TypeError("provider binder returned an invalid receipt")
            normalized_receipt = ProviderReceiptEnvelopeV1(
                provider_id=normalized_receipt.provider_id,
                media_type=normalized_receipt.media_type,
                payload=normalized_receipt.payload,
            )
        bound = BoundProviderOperationsV1(
            provider_id=supplied.provider_id,
            factory=supplied.factory,
            model_lister=supplied.model_lister,
            doctor=supplied.doctor,
            normalized_receipt=normalized_receipt,
            provider_home=supplied.provider_home,
        )
        if bound.provider_id != plugin.id:
            raise ValueError("provider binder id changed")
    except _CANCELLATION_EXCEPTIONS:
        raise
    except BaseException:
        failed = True
        bound = None
    if failed:
        raise _plugin_error(plugin.id, "bind") from None
    assert bound is not None
    return bound


def _configured_operations(
    plugin: ProviderPluginV1,
    extension_launch: Optional["ExtensionLaunchOverridesV1"],
    *,
    required: bool,
) -> Optional[BoundProviderOperationsV1]:
    if plugin.configuration_abi_version is None:
        if required or extension_launch is not None:
            raise _plugin_error(plugin.id, "configure") from None
        return None
    context = _launch_context_for_plugin(plugin, extension_launch)
    return _bind_plugin_operations(plugin, context)


def bind_extension_provider(
    provider_id: ProviderId,
    *,
    extension_launch: Optional["ExtensionLaunchOverridesV1"] = None,
) -> BoundProviderOperationsV1:
    """Return Core-reconstructed operations for a configured extension."""

    plugin = load_provider_plugin(provider_id)
    _require_executable_support(plugin)
    bound = _configured_operations(
        plugin, extension_launch, required=True,
    )
    assert bound is not None
    return bound


def _create_request(
    plugin: ProviderPluginV1,
    model: Optional[str],
    opts: Mapping[str, Any],
) -> ProviderCreateRequestV1:
    values = dict(opts)
    workspace = values.pop("cwd", None)
    if "web_search" in values:
        web_search = values.pop("web_search")
        if type(web_search) is not bool or web_search:
            raise ValueError("configured extensions do not accept web_search")
    if "first_output_timeout" in values:
        if values.pop("first_output_timeout") is not None:
            raise ValueError("configured extensions do not accept first_output_timeout")
    limit_names = (
        "timeout",
        "max_output_bytes",
        "max_stderr_bytes",
        "max_stream_buffer_bytes",
        "max_stream_events",
        "max_stream_line_bytes",
    )
    limits = {name: values.pop(name, None) for name in limit_names}
    if values:
        raise ValueError("configured extension received unsupported options")
    return ProviderCreateRequestV1(
        provider_id=plugin.id,
        model=plugin.default_model if model is None else model,
        workspace=workspace,
        **limits,
    )


def instantiate_extension_provider(
    provider_id: ProviderId,
    *,
    model: Optional[str] = None,
    extension_launch: Optional["ExtensionLaunchOverridesV1"] = None,
    **opts: Any,
) -> BaseProvider:
    plugin = load_provider_plugin(provider_id)
    _require_executable_support(plugin)
    request = None
    if plugin.configuration_abi_version is not None:
        try:
            # Reject malformed Core options before a binder can touch the
            # filesystem, invoke provider code, or retain launch context.
            request = _create_request(plugin, model, opts)
        except _CANCELLATION_EXCEPTIONS:
            raise
        except BaseException:
            raise _plugin_error(provider_id, "factory") from None
    bound = _configured_operations(
        plugin, extension_launch, required=False,
    )
    failed = False
    try:
        if bound is None:
            provider = plugin.factory(model=model or plugin.default_model, **opts)
        else:
            assert request is not None
            provider = bound.factory(request)
        if not isinstance(provider, BaseProvider) or provider.name != provider_id:
            raise TypeError("provider factory returned an incompatible object")
        wrapped = _ExtensionProviderProxy(provider_id, provider)
    except _CANCELLATION_EXCEPTIONS:
        raise
    except BaseException:
        failed = True
        provider = None
        wrapped = None
    if failed:
        raise _plugin_error(provider_id, "factory") from None
    assert wrapped is not None
    return wrapped


def list_extension_models(
    provider_id: ProviderId,
    *,
    extension_launch: Optional["ExtensionLaunchOverridesV1"] = None,
) -> list[ModelInfo]:
    """Run one explicitly requested extension's model lister safely."""
    plugin = load_provider_plugin(provider_id)
    _require_executable_support(plugin)
    bound = _configured_operations(
        plugin, extension_launch, required=False,
    )
    failed = False
    try:
        # Materialize at most one item past the public limit so a buggy
        # infinite generator cannot grow memory without bound.
        supplied = list(itertools.islice(
            iter(
                plugin.model_lister()
                if bound is None
                else bound.model_lister()
            ),
            _MAX_EXTENSION_MODELS + 1,
        ))
        if len(supplied) > _MAX_EXTENSION_MODELS:
            raise ValueError("provider model_lister returned too many models")

        models: list[ModelInfo] = []
        seen_ids: set[str] = set()
        default_count = 0
        for model in supplied:
            if type(model) is not ModelInfo:
                raise TypeError("provider model_lister returned invalid metadata")
            if not _valid_plugin_text(
                model.id,
                max_chars=_MAX_MODEL_ID_CHARS,
                allow_empty=False,
                require_trimmed=True,
            ):
                raise ValueError("provider model id is invalid")
            if type(model.provider) is not str or model.provider != provider_id:
                raise ValueError("provider model id namespace is invalid")
            if not _valid_plugin_text(
                model.display_name,
                max_chars=_MAX_MODEL_DISPLAY_CHARS,
                allow_empty=True,
                require_trimmed=False,
            ):
                raise ValueError("provider model display name is invalid")
            if type(model.default) is not bool or type(model.deprecated) is not bool:
                raise TypeError("provider model flags must be bool")
            if type(model.source) is not str or model.source != "plugin":
                raise ValueError("extension model source must be plugin")
            if model.id in seen_ids:
                raise ValueError("provider model ids must be unique")
            seen_ids.add(model.id)
            default_count += int(model.default)
            if default_count > 1:
                raise ValueError("provider model list has multiple defaults")

            # Return core-owned copies so a plugin cannot mutate already
            # validated metadata through retained references.
            models.append(ModelInfo(
                id=model.id,
                provider=provider_id,
                display_name=model.display_name,
                default=model.default,
                deprecated=model.deprecated,
                source="plugin",
            ))
    except _CANCELLATION_EXCEPTIONS:
        raise
    except BaseException:
        failed = True
        models = []
    if failed:
        raise _plugin_error(provider_id, "models") from None
    return models


def _copy_doctor_result(value: Any) -> Any:
    """Return bounded Core-owned JSON data from one extension doctor."""

    budget = [_MAX_DOCTOR_ITEMS, _MAX_DOCTOR_TEXT_BYTES]

    def copy(item: Any, depth: int) -> Any:
        if depth > _MAX_DOCTOR_DEPTH:
            raise ValueError("provider doctor result is nested too deeply")
        budget[0] -= 1
        if budget[0] < 0:
            raise ValueError("provider doctor result has too many values")
        if item is None or type(item) in (bool, int):
            if type(item) is int and item.bit_length() > 4096:
                raise ValueError("provider doctor integer is too large")
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError("provider doctor number is invalid")
            return item
        if type(item) is str:
            encoded = item.encode("utf-8", "strict")
            budget[1] -= len(encoded)
            if budget[1] < 0:
                raise ValueError("provider doctor text is too large")
            return item
        if type(item) in (list, tuple):
            if len(item) > _MAX_DOCTOR_ITEMS:
                raise ValueError("provider doctor collection is too large")
            return [copy(child, depth + 1) for child in item]
        if not isinstance(item, Mapping):
            raise TypeError("provider doctor result must contain JSON values")
        result = {}
        for index, pair in enumerate(item.items()):
            if index >= _MAX_DOCTOR_ITEMS:
                raise ValueError("provider doctor mapping is too large")
            key, child = pair
            if type(key) is not str or not key or len(key) > 128:
                raise ValueError("provider doctor key is invalid")
            encoded_key = key.encode("utf-8", "strict")
            budget[1] -= len(encoded_key)
            if budget[1] < 0:
                raise ValueError("provider doctor text is too large")
            result[key] = copy(child, depth + 1)
        return result

    return copy(value, 0)


def _run_extension_doctor(
    plugin: ProviderPluginV1,
    bound: Optional[BoundProviderOperationsV1],
) -> Any:
    failed = False
    try:
        supplied = plugin.doctor() if bound is None else bound.doctor()
        # ABI-v1 legacy doctor returns Any and remains exact. The additive
        # bound configuration ABI crosses the new Core-owned data boundary.
        result = supplied if bound is None else _copy_doctor_result(supplied)
    except _CANCELLATION_EXCEPTIONS:
        raise
    except BaseException:
        failed = True
        result = None
    if failed:
        raise _plugin_error(plugin.id, "doctor") from None
    return result


def doctor_provider(
    provider_id: ProviderId,
    *,
    extension_launch: Optional["ExtensionLaunchOverridesV1"] = None,
) -> Any:
    """Run a built-in or explicitly requested extension provider doctor."""
    if provider_id in BUILTIN_PROVIDER_IDS:
        if extension_launch is not None:
            raise _plugin_error(provider_id, "configure") from None
        from .ui import collect_states

        return next(state for state in collect_states() if state.name == provider_id)

    plugin = load_provider_plugin(provider_id)
    _require_executable_support(plugin)
    bound = _configured_operations(
        plugin, extension_launch, required=False,
    )
    return _run_extension_doctor(plugin, bound)


def configure_extension_provider(
    provider_id: ProviderId,
    extension_launch: Optional["ExtensionLaunchOverridesV1"] = None,
    *,
    verify: bool = True,
) -> "StoredExtensionLaunchV1":
    """Verify and persist one normalized configuration-capable receipt."""

    if type(verify) is not bool:
        raise _plugin_error(provider_id, "configure") from None
    plugin = load_provider_plugin(provider_id)
    _require_executable_support(plugin)
    bound = _configured_operations(
        plugin, extension_launch, required=True,
    )
    assert bound is not None
    if verify:
        result = _run_extension_doctor(plugin, bound)
        unavailable = False
        invalid = False
        try:
            if isinstance(result, Mapping) and "available" in result:
                available = result["available"]
                invalid = type(available) is not bool
                unavailable = available is False
        except _CANCELLATION_EXCEPTIONS:
            raise
        except BaseException:
            invalid = True
        if invalid or unavailable:
            raise _plugin_error(provider_id, "configure") from None
    if bound.normalized_receipt is None:
        raise _plugin_error(provider_id, "configure") from None
    try:
        from .extension_config import save_extension_launch

        return save_extension_launch(
            provider_id,
            bound.normalized_receipt,
            provider_home=bound.provider_home,
        )
    except _CANCELLATION_EXCEPTIONS:
        raise
    except BaseException:
        raise _plugin_error(provider_id, "configure") from None


def clear_extension_provider_configuration(provider_id: ProviderId) -> bool:
    """Clear persisted Core launch state without importing a provider plugin."""

    try:
        from .extension_config import clear_extension_launch

        return clear_extension_launch(provider_id)
    except _CANCELLATION_EXCEPTIONS:
        raise
    except BaseException:
        raise _plugin_error(provider_id, "configure") from None


def _builtin_descriptors() -> list[ProviderDescriptorV1]:
    # Imported only for an explicit registry listing, never for create/route.
    from .models import DEFAULT_MODELS

    return [
        ProviderDescriptorV1(
            id=provider_id,
            source="builtin",
            status="builtin",
            support_status="stable",
            default_model=DEFAULT_MODELS[provider_id],
            route_prefixes=(provider_id,),
            server_policy=ProviderServerPolicyV1(
                enabled=(provider_id == "claude"),
                requires_external_isolation=(provider_id != "claude"),
            ),
        )
        for provider_id in BUILTIN_PROVIDER_IDS
    ]


def passive_bundled_provider_descriptors() -> Tuple[ProviderDescriptorV1, ...]:
    """Return callback-free Preview descriptors for bundled entry points.

    The entry-point target is deliberately validated but not imported.  Model
    defaults and capabilities belong to provider code, so this passive view
    does not guess them; an explicit manage action may load exactly one target.
    """

    descriptors = []
    seen = set()
    prefix = "unified_cli_ext.providers."
    for provider_id, target in BUNDLED_EXTENSION_ENTRY_POINTS_V1:
        if (
            provider_id in seen
            or provider_id in RESERVED_PROVIDER_IDS
            or not _valid_provider_id(provider_id)
            or type(target) is not str
            or not target.startswith(prefix)
            or not target.endswith(":PLUGIN")
        ):
            raise RuntimeError("bundled provider entry-point metadata is invalid")
        seen.add(provider_id)
        descriptors.append(ProviderDescriptorV1(
            id=provider_id,
            source="extension",
            status="discovered",
            support_status="preview",
            default_model=None,
            capabilities=frozenset(),
            route_prefixes=(provider_id,),
            server_policy=ProviderServerPolicyV1(
                enabled=False,
                requires_external_isolation=True,
            ),
        ))
    return tuple(descriptors)


def _descriptor_from_loaded_plugin(
    plugin: ProviderPluginV1,
) -> ProviderDescriptorV1:
    """Reconstruct one callback-free descriptor from validated plugin metadata.

    Both explicit snapshots and registry listings pass through this helper so
    the two views cannot drift.  The returned frozen dataclass contains only
    Core-owned immutable values; no provider callback is retained.
    """

    policy = plugin.server_policy
    copied_policy = (
        None
        if policy is None
        else ProviderServerPolicyV1(
            enabled=policy.enabled,
            requires_external_isolation=policy.requires_external_isolation,
        )
    )
    return ProviderDescriptorV1(
        id=plugin.id,
        source="extension",
        status="loaded",
        support_status=plugin.support_status,
        default_model=plugin.default_model,
        capabilities=plugin.capabilities,
        route_prefixes=plugin.route_prefixes,
        server_policy=copied_policy,
    )


def snapshot_provider_descriptor(provider_id: ProviderId) -> ProviderDescriptorV1:
    """Load and snapshot exactly one explicitly requested extension provider.

    This invokes only the matching entry-point loader and the Core metadata
    validator.  Held providers are rejected before a descriptor is returned;
    factory, model-lister, doctor, and launch-binder callbacks are never run.
    """

    plugin = load_provider_plugin(provider_id)
    _require_executable_support(plugin)
    return _descriptor_from_loaded_plugin(plugin)


def list_providers(*, include_ext: bool = False) -> list[ProviderDescriptorV1]:
    """Return immutable descriptors; extensions are metadata-only by default.

    Even with ``include_ext=True``, entry points are enumerated but their
    modules are not imported.  Plugins explicitly loaded earlier may expose
    their already-validated metadata in the returned descriptor.
    """
    descriptors = _builtin_descriptors()
    if not include_ext or plugins_disabled():
        return descriptors

    entry_points = _discover_entry_points()
    bundled = {
        descriptor.id: descriptor
        for descriptor in passive_bundled_provider_descriptors()
    }
    by_name: Dict[str, list[Any]] = {}
    invalid_names: list[str] = []
    for entry_point in entry_points:
        name = _safe_entry_point_name(entry_point)
        if name is not None:
            by_name.setdefault(name, []).append(entry_point)
        else:
            invalid_names.append("<invalid>")

    for provider_id in sorted(by_name):
        entries = by_name[provider_id]
        error: Optional[str] = None
        status: Literal["discovered", "loaded", "invalid", "broken"] = (
            "discovered"
        )
        if provider_id in RESERVED_PROVIDER_IDS:
            status, error = "invalid", "reserved_id"
        elif len(entries) > 1:
            status, error = "invalid", "duplicate_entry_point"

        with _LOCK:
            record = _LOADS.get(provider_id)
            loaded = record.plugin if record and record.ready.is_set() else None
            failed = record.error if record and record.ready.is_set() else None
        if loaded is not None:
            status = "loaded"
            descriptor = _descriptor_from_loaded_plugin(loaded)
            if loaded.support_status == "held":
                # Metadata listing remains allowed for a plugin explicitly
                # loaded through another registry API.  It must not expose an
                # executable default while the support gate is Held.
                descriptor = ProviderDescriptorV1(
                    id=descriptor.id,
                    source=descriptor.source,
                    status=descriptor.status,
                    support_status=descriptor.support_status,
                    default_model=None,
                    capabilities=descriptor.capabilities,
                    route_prefixes=descriptor.route_prefixes,
                    server_policy=descriptor.server_policy,
                    error=descriptor.error,
                )
            descriptors.append(descriptor)
        else:
            if failed is not None and status == "discovered":
                status, error = "broken", "load_failed"
            passive = bundled.get(provider_id)
            if (
                passive is not None
                and status == "discovered"
                and error is None
                and len(entries) == 1
            ):
                # The bundled manifest is generated from this distribution's
                # entry-point metadata.  Reuse its callback-free Preview
                # descriptor so CLI, REPL, and manage surfaces agree without
                # importing provider code or probing a vendor executable.
                descriptors.append(passive)
            else:
                descriptors.append(ProviderDescriptorV1(
                    id=provider_id,
                    source="extension",
                    status=status,
                    error=error,
                ))

    for invalid_name in invalid_names:
        descriptors.append(ProviderDescriptorV1(
            id=invalid_name,
            source="extension",
            status="invalid",
            error="invalid_entry_point_name",
        ))
    return descriptors


def _reset_provider_registry_for_tests() -> None:
    """Clear discovery/load caches.  Test suites must not call during a load."""
    global _ENTRY_POINTS, _DISCOVERY_FAILED
    with _LOCK:
        _ENTRY_POINTS = None
        _DISCOVERY_FAILED = False
        _LOADS.clear()


__all__ = [
    "BUILTIN_PROVIDER_IDS",
    "DISABLE_PLUGINS_ENV",
    "ENTRY_POINT_GROUP",
    "ProviderDescriptorV1",
    "ProviderDescriptor",
    "RESERVED_PROVIDER_IDS",
    "bind_extension_provider",
    "clear_extension_provider_configuration",
    "configure_extension_provider",
    "extension_provider_exists",
    "doctor_provider",
    "instantiate_extension_provider",
    "list_extension_models",
    "list_providers",
    "load_provider_plugin",
    "plugins_disabled",
    "snapshot_provider_descriptor",
]
