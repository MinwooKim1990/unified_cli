"""Mutable, provider-neutral state for the interactive REPL.

``ReplState`` deliberately contains only UI/runtime preferences.  Provider
capabilities are applied by the dispatcher when Core actually supports them;
keeping a value here must never be mistaken for changing an external CLI.
The small legacy bridge lets older callers and tests continue to pass the
historical ``current`` and ``provider_opts`` dictionaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Optional

from .core import ModelInfo

if TYPE_CHECKING:
    from .registry import ProviderDescriptorV1


@dataclass
class ReplState:
    provider: str
    model: str
    cwd: str = ""
    web_search: bool = True
    web_explicit: bool = False
    permission_mode: str = "provider_default"
    context_window: int = 8
    timeout: Optional[float] = None
    style: str = "default"
    effort: str = "default"
    system_prompt: Optional[str] = None
    reasoning_summaries: bool = False
    theme: str = "auto"
    multiline: bool = True
    pending_images: list[str] = field(default_factory=list)
    added_dirs: list[str] = field(default_factory=list)
    last_latency_ms: int = 0
    # Process-local extension metadata.  These mappings are mirrored only into
    # the legacy ``current`` dictionary used by the active REPL/completer; they
    # are never written to settings or session files.
    loaded_extension_descriptors: dict[str, "ProviderDescriptorV1"] = field(
        default_factory=dict
    )
    loaded_extension_models: dict[str, tuple[ModelInfo, ...]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        self.loaded_extension_descriptors = _copy_descriptor_snapshots(
            self.loaded_extension_descriptors
        )
        self.loaded_extension_models = _copy_model_snapshots(
            self.loaded_extension_models
        )
        for provider, descriptor in self.loaded_extension_descriptors.items():
            if provider not in self.loaded_extension_models and (
                descriptor.default_model is not None
            ):
                self.loaded_extension_models[provider] = (ModelInfo(
                    id=descriptor.default_model,
                    provider=provider,
                    default=True,
                    source="plugin",
                ),)

    @classmethod
    def from_legacy(
        cls,
        current: dict,
        provider_opts: Optional[dict] = None,
        pending_images: Optional[list[str]] = None,
        *,
        context_window: int = 8,
    ) -> "ReplState":
        """Create state from the dictionaries accepted by REPL v1.

        Extra keys written by :meth:`sync_legacy` make settings survive when a
        compatibility caller invokes ``_handle_slash`` repeatedly without
        retaining the ``ReplState`` instance itself.
        """
        opts = provider_opts or {}
        return cls(
            provider=str(current.get("provider") or "claude"),
            model=str(current.get("model") or ""),
            cwd=str(opts.get("cwd") or current.get("cwd") or ""),
            web_search=bool(opts.get("web_search", current.get("web_search", True))),
            web_explicit=bool(current.get("web_explicit", False)),
            permission_mode=_safe_permission_mode(current.get("permission_mode")),
            context_window=_safe_positive_int(
                current.get("context_window"), context_window
            ),
            timeout=_safe_timeout(opts.get("timeout", current.get("timeout"))),
            style=str(current.get("style") or "default"),
            effort=str(current.get("effort") or "default"),
            system_prompt=_safe_optional_text(current.get("system_prompt")),
            reasoning_summaries=bool(current.get("reasoning_summaries", False)),
            theme=str(current.get("theme") or "auto"),
            multiline=bool(current.get("multiline", True)),
            pending_images=pending_images if pending_images is not None else [],
            added_dirs=list(current.get("added_dirs") or []),
            last_latency_ms=_safe_nonnegative_int(current.get("last_latency_ms"), 0),
            loaded_extension_descriptors=_copy_descriptor_snapshots(
                current.get("loaded_extension_descriptors")
            ),
            loaded_extension_models=_copy_model_snapshots(
                current.get("loaded_extension_models")
            ),
        )

    def sync_legacy(self, current: dict, provider_opts: Optional[dict] = None) -> None:
        """Reflect live state into v1 dictionaries and the toolbar mapping."""
        current.update({
            "provider": self.provider,
            "model": self.model,
            "cwd": self.cwd,
            "web_search": self.web_search,
            "web_explicit": self.web_explicit,
            "permission_mode": self.permission_mode,
            "context_window": self.context_window,
            "timeout": self.timeout,
            "style": self.style,
            "effort": self.effort,
            "system_prompt": self.system_prompt,
            "reasoning_summaries": self.reasoning_summaries,
            "theme": self.theme,
            "multiline": self.multiline,
            "added_dirs": list(self.added_dirs),
            "last_latency_ms": self.last_latency_ms,
            "loaded_extension_descriptors": dict(
                self.loaded_extension_descriptors
            ),
            "loaded_extension_models": {
                provider: tuple(_copy_model(model) for model in models)
                for provider, models in self.loaded_extension_models.items()
            },
        })
        if provider_opts is not None:
            provider_opts["cwd"] = self.cwd
            if self.provider in {"claude", "codex", "gemini"}:
                provider_opts["web_search"] = self.web_search
            else:
                provider_opts.pop("web_search", None)
            if self.timeout is None:
                provider_opts.pop("timeout", None)
            else:
                provider_opts["timeout"] = self.timeout

    def remember_extension_descriptor(
        self, descriptor: "ProviderDescriptorV1"
    ) -> None:
        """Retain one Core-owned frozen descriptor for this process only."""

        copied = _copy_descriptor(descriptor)
        if copied is None:
            raise ValueError("invalid extension descriptor snapshot")
        self.loaded_extension_descriptors[copied.id] = copied
        if (
            copied.id not in self.loaded_extension_models
            and copied.default_model is not None
        ):
            # Descriptor metadata provides a minimal no-probe model view.
            self.loaded_extension_models[copied.id] = (ModelInfo(
                id=copied.default_model,
                provider=copied.id,
                default=True,
                source="plugin",
            ),)

    def replace_extension_models(
        self, provider: str, models: Iterable[ModelInfo]
    ) -> None:
        """Atomically replace one provider's last-good Core model snapshot."""

        copied = tuple(_copy_model(model) for model in models)
        if any(model.provider != provider for model in copied):
            raise ValueError("extension model snapshot provider mismatch")
        if not copied:
            descriptor = self.loaded_extension_descriptors.get(provider)
            if descriptor is not None and descriptor.default_model is not None:
                # A successful empty refresh is authoritative about discovered
                # alternatives, but the descriptor's validated default remains
                # the minimum explicit, no-probe selection view.
                copied = (ModelInfo(
                    id=descriptor.default_model,
                    provider=provider,
                    default=True,
                    source="plugin",
                ),)
        self.loaded_extension_models[provider] = copied

    def extension_models(self, provider: str) -> tuple[ModelInfo, ...]:
        return tuple(
            _copy_model(model)
            for model in self.loaded_extension_models.get(provider, ())
        )

    def summary(self) -> dict[str, Any]:
        """Return non-secret state suitable for ``/settings`` display."""
        return {
            "provider": self.provider,
            "model": self.model,
            "cwd": self.cwd,
            "web": (
                ("on" if self.web_search else "off")
                if self.web_explicit else "default"
            ),
            "permission": self.permission_mode,
            "context": self.context_window,
            "timeout": "default" if self.timeout is None else self.timeout,
            "style": self.style,
            "effort": self.effort,
            "system": (
                "default" if self.system_prompt is None
                else "configured (" + str(len(self.system_prompt)) + " chars)"
            ),
            "reasoning": "public summaries" if self.reasoning_summaries else "hidden",
            "theme": self.theme,
            "multiline": "on" if self.multiline else "off",
        }


def _safe_positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if parsed > 0 else default


def _safe_nonnegative_int(value: object, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if parsed >= 0 else default


def _safe_timeout(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _safe_permission_mode(value: object) -> str:
    # Translate the short-lived Stage 3 preview spelling, but never retain the
    # removed ``full`` mode or arbitrary provider-specific values.
    if value == "provider-default":
        return "provider_default"
    if value in {"provider_default", "read_only", "workspace_write"}:
        return str(value)
    return "provider_default"


def _safe_optional_text(value: object) -> Optional[str]:
    return value if type(value) is str else None


def _copy_descriptor(value: object) -> Optional["ProviderDescriptorV1"]:
    # Importing the registry module does not enumerate entry points.  Keeping
    # the import local avoids making this state container own discovery policy.
    from .plugin import ProviderServerPolicyV1
    from .registry import ProviderDescriptorV1

    if type(value) is not ProviderDescriptorV1:
        return None
    if value.source != "extension" or value.status != "loaded":
        return None
    policy = value.server_policy
    copied_policy = (
        None
        if policy is None
        else ProviderServerPolicyV1(
            enabled=policy.enabled,
            requires_external_isolation=policy.requires_external_isolation,
        )
    )
    return ProviderDescriptorV1(
        id=value.id,
        source="extension",
        status="loaded",
        default_model=value.default_model,
        capabilities=frozenset(value.capabilities),
        route_prefixes=tuple(value.route_prefixes),
        server_policy=copied_policy,
        error=value.error,
        support_status=value.support_status,
    )


def _copy_descriptor_snapshots(
    value: object,
) -> dict[str, "ProviderDescriptorV1"]:
    if type(value) is not dict:
        return {}
    snapshots = {}
    for provider, descriptor in value.items():
        copied = _copy_descriptor(descriptor)
        if type(provider) is str and copied is not None and copied.id == provider:
            snapshots[provider] = copied
    return snapshots


def _copy_model(value: ModelInfo) -> ModelInfo:
    if type(value) is not ModelInfo:
        raise ValueError("invalid extension model snapshot")
    return ModelInfo(
        id=value.id,
        provider=value.provider,
        display_name=value.display_name,
        default=value.default,
        deprecated=value.deprecated,
        source=value.source,
    )


def _copy_model_snapshots(value: object) -> dict[str, tuple[ModelInfo, ...]]:
    if type(value) is not dict:
        return {}
    snapshots = {}
    for provider, models in value.items():
        if type(provider) is not str or type(models) not in (tuple, list):
            continue
        try:
            copied = tuple(_copy_model(model) for model in models)
        except ValueError:
            continue
        if all(model.provider == provider for model in copied):
            snapshots[provider] = copied
    return snapshots
