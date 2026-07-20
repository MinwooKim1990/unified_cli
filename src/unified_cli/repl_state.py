"""Mutable, provider-neutral state for the interactive REPL.

``ReplState`` deliberately contains only UI/runtime preferences.  Provider
capabilities are applied by the dispatcher when Core actually supports them;
keeping a value here must never be mistaken for changing an external CLI.
The small legacy bridge lets older callers and tests continue to pass the
historical ``current`` and ``provider_opts`` dictionaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


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
        })
        if provider_opts is not None:
            provider_opts["cwd"] = self.cwd
            provider_opts["web_search"] = self.web_search
            if self.timeout is None:
                provider_opts.pop("timeout", None)
            else:
                provider_opts["timeout"] = self.timeout

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
