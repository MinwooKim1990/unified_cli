"""Dynamic model listing across providers.

Strategy per provider:
  claude : GET https://api.anthropic.com/v1/models (needs ANTHROPIC_API_KEY).
           Fallback: hardcoded aliases. Wrapper always accepts arbitrary model
           IDs — the list is informational.
  codex  : read ~/.codex/models_cache.json (CLI maintains this, ~5min TTL).
           Fallback: hardcoded subscription-safe models.
  gemini : GET https://generativelanguage.googleapis.com/v1/models?key=...
           (needs GEMINI_API_KEY). Fallback: hardcoded.

In-memory TTL cache (1 hour) so `list_models()` in a long-running server
doesn't hammer the upstream API.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Optional

from .core import ModelInfo, ProviderName


DEFAULT_MODELS: dict[ProviderName, str] = {
    "claude": "claude-haiku-4-5",
    "codex": "gpt-5.4-mini",
    "gemini": "gemini-3.1-flash-lite-preview",
}

_HARDCODED: dict[ProviderName, list[str]] = {
    "claude": [
        "claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-5",
        "haiku", "sonnet", "opus",
    ],
    "codex": [
        "gpt-5.4-mini", "gpt-5.4", "gpt-5.2", "gpt-5.3-codex-spark",
    ],
    "gemini": [
        "gemini-3.1-flash-lite-preview", "gemini-3.1-flash",
        "gemini-3.1-pro", "gemini-2.5-flash-lite",
    ],
}

_CACHE: dict[ProviderName, tuple[float, list[ModelInfo]]] = {}
_TTL = 3600.0  # 1 hour


def _cached(provider: ProviderName) -> Optional[list[ModelInfo]]:
    entry = _CACHE.get(provider)
    if not entry:
        return None
    ts, items = entry
    if time.time() - ts > _TTL:
        return None
    return items


def _store(provider: ProviderName, items: list[ModelInfo]) -> list[ModelInfo]:
    _CACHE[provider] = (time.time(), items)
    return items


def _mark_defaults(items: list[ModelInfo], provider: ProviderName) -> list[ModelInfo]:
    default = DEFAULT_MODELS.get(provider)
    for it in items:
        if it.id == default:
            it.default = True
    return items


def _hardcoded(provider: ProviderName) -> list[ModelInfo]:
    return _mark_defaults(
        [ModelInfo(id=m, provider=provider, source="hardcoded") for m in _HARDCODED[provider]],
        provider,
    )


# ---- Claude ----

def _list_claude() -> list[ModelInfo]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return _hardcoded("claude")
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/models?limit=100",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.load(resp)
        items = [
            ModelInfo(
                id=m.get("id"), display_name=m.get("display_name", ""),
                provider="claude", source="api",
            )
            for m in (data.get("data") or [])
            if m.get("id")
        ]
        return _mark_defaults(items, "claude") if items else _hardcoded("claude")
    except Exception:
        return _hardcoded("claude")


# ---- Codex ----

def _list_codex() -> list[ModelInfo]:
    cache_path = Path("~/.codex/models_cache.json").expanduser()
    if not cache_path.exists():
        return _hardcoded("codex")
    try:
        with cache_path.open() as f:
            data = json.load(f)
        items = [
            ModelInfo(
                id=m.get("slug"), display_name=m.get("display_name", ""),
                provider="codex", source="cache",
            )
            for m in (data.get("models") or [])
            if m.get("slug")
        ]
        return _mark_defaults(items, "codex") if items else _hardcoded("codex")
    except Exception:
        return _hardcoded("codex")


# ---- Gemini ----

def _list_gemini() -> list[ModelInfo]:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        return _hardcoded("gemini")
    try:
        url = f"https://generativelanguage.googleapis.com/v1/models?key={key}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.load(resp)
        items: list[ModelInfo] = []
        for m in data.get("models") or []:
            raw_name = m.get("name") or ""  # "models/gemini-3.1-flash"
            mid = raw_name.split("/", 1)[-1] if raw_name else ""
            if mid:
                items.append(ModelInfo(
                    id=mid,
                    display_name=m.get("displayName", ""),
                    provider="gemini", source="api",
                ))
        return _mark_defaults(items, "gemini") if items else _hardcoded("gemini")
    except Exception:
        return _hardcoded("gemini")


_LISTERS = {"claude": _list_claude, "codex": _list_codex, "gemini": _list_gemini}


def list_models(
    provider: Optional[ProviderName] = None,
    *,
    force_refresh: bool = False,
) -> list[ModelInfo]:
    """Return available models for a provider, or all providers combined."""
    if provider:
        if not force_refresh:
            cached = _cached(provider)
            if cached is not None:
                return cached
        return _store(provider, _LISTERS[provider]())

    out: list[ModelInfo] = []
    for p in ("claude", "codex", "gemini"):  # type: ignore[assignment]
        out += list_models(p, force_refresh=force_refresh)
    return out
