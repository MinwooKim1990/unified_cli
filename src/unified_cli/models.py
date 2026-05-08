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

# Hardcoded fallback model IDs, used when the official API isn't reachable
# (no API key set, or the request fails). These are the IDs verified end-to-end
# via Phase 0 live testing — see PHASE0_VERIFICATION.md.
#
# Single source of truth for both `unified-cli models` output AND the hint text
# in errors.py; do not duplicate elsewhere.
_HARDCODED: dict[ProviderName, list[str]] = {
    "claude": [
        # 2026-04 GA tiers (verified live; aliases map to these snapshots)
        "claude-opus-4-7",       # latest flagship (GA 2026-04-16)
        "claude-sonnet-4-6",     # GA 2026-02-17
        "claude-haiku-4-5",      # GA — fastest tier (this wrapper's default)
        # Aliases — Claude CLI maps these to the latest snapshot of each tier.
        "haiku", "sonnet", "opus",
    ],
    "codex": [
        # Verified live on ChatGPT subscription auth.
        # `gpt-5.5` is the true flagship per ~/.codex/models_cache.json but
        # requires an upgraded codex CLI; users on older CLIs see a clear
        # "requires newer Codex" error and can `brew upgrade codex`.
        "gpt-5.5",                 # frontier (needs codex >= ~0.130)
        "gpt-5.4",                 # strong everyday
        "gpt-5.4-mini",            # fastest mini (this wrapper's default)
        "gpt-5.3-codex",           # coding-specialized
        "gpt-5.3-codex-spark",     # lightweight, fastest
        "gpt-5.2",                 # older flagship
        "codex-auto-review",       # review specialist
    ],
    "gemini": [
        # NOTE: Bare `gemini-3.1-pro` and `gemini-3.1-flash` (without
        # `-preview`) do NOT exist — they 404 against the API. The real
        # current IDs are below. See PHASE0_VERIFICATION.md for details.
        "gemini-3.1-pro-preview",          # current flagship
        "gemini-3-flash-preview",          # 3.x flash (note: not "3.1")
        "gemini-3.1-flash-lite-preview",   # default
        "gemini-3.1-flash-lite",           # stable promotion
        # Legacy 2.5 line — scheduled for shutdown 2026-10-16 per Google docs.
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
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
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
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
    key = (
        (os.environ.get("GEMINI_API_KEY") or "").strip()
        or (os.environ.get("GOOGLE_API_KEY") or "").strip()
    )
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
