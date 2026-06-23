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
    # "gemini" now wraps the Antigravity `agy` CLI. `agy --model` accepts both
    # slugs ("gemini-3.5-flash") and display names; the slug routes through
    # factory.route()'s `^gemini` regex, so we default to the slug.
    "gemini": "gemini-3.5-flash",
}

# Hardcoded fallback model IDs, used when the official API isn't reachable
# (no API key set, or the request fails). These are the IDs verified end-to-end
# via Phase 0 live testing against each CLI.
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
        # Antigravity `agy` models. `agy --model` accepts these slug forms as
        # well as the display names shown by `agy models`. Default fallback if
        # `agy models` can't be run. Multi-family models (Claude / GPT-OSS) are
        # also routable through agy but listed under their display names from
        # `agy models` at runtime.
        "gemini-3.5-flash",   # default
        "gemini-3.1-pro",
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


# ---- Gemini / Antigravity (agy) ----

def _list_gemini() -> list[ModelInfo]:
    """List models via `agy models`. The display names it prints (e.g.
    "Gemini 3.5 Flash (Medium)", "Claude Sonnet 4.6 (Thinking)") are valid
    `--model` values. Falls back to the hardcoded slug list if `agy` isn't
    found or the call fails.
    """
    import subprocess
    from .discovery import find_agy_bin

    agy = find_agy_bin()
    if not agy:
        return _hardcoded("gemini")
    try:
        out = subprocess.run(
            [agy, "models"], capture_output=True, text=True, input="", timeout=15,
        )
        if out.returncode != 0:
            return _hardcoded("gemini")
        items: list[ModelInfo] = []
        for line in out.stdout.splitlines():
            name = line.strip()
            if not name:
                continue
            items.append(ModelInfo(
                id=name, display_name=name, provider="gemini", source="cache",
            ))
        # Also expose the convenient slugs so `-m gemini-3.5-flash` keeps
        # working and route()'s ^gemini regex matches.
        for slug in _HARDCODED["gemini"]:
            if not any(i.id == slug for i in items):
                items.append(ModelInfo(
                    id=slug, provider="gemini", source="hardcoded",
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
