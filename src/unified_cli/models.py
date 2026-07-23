"""Dynamic model listing across providers.

Strategy per provider:
  claude : GET https://api.anthropic.com/v1/models (needs ANTHROPIC_API_KEY).
           Fallback: hardcoded aliases. Wrapper always accepts arbitrary model
           IDs — the list is informational.
  codex  : read ~/.codex/models_cache.json (CLI maintains this, ~5min TTL).
           Fallback: hardcoded subscription-safe models.
  gemini : run the opt-in Antigravity ``agy models`` command. Fallback:
           hardcoded slugs when disabled, unavailable, or unsuccessful.

Context-aware bounded in-memory TTL cache (1 hour) so `list_models()` in a
long-running server does not hammer an upstream API, file, or executable.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.request
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterable, Optional, Tuple

from .core import ModelInfo, ProviderId, ProviderName
from .errors import UnifiedError

if TYPE_CHECKING:
    from .extension_config import ExtensionLaunchOverridesV1


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

# Cache records contain primitives only.  ``ModelInfo`` is intentionally a
# mutable public dataclass, so retaining or returning caller-owned instances
# would let one request corrupt later results.
_ModelRecord = Tuple[str, ProviderId, str, bool, bool, str]
_ModelContext = str
_ModelCacheKey = Tuple[ProviderName, _ModelContext]
_ModelFlightKey = Tuple[ProviderName, int, _ModelContext]
_CACHE: "OrderedDict[_ModelCacheKey, Tuple[float, Tuple[_ModelRecord, ...]]]" = OrderedDict()
_TTL = 3600.0  # 1 hour
_CACHE_LOCK = threading.RLock()
_CACHE_FLIGHTS: Dict[_ModelFlightKey, "_ModelFlight"] = {}
_CACHE_GENERATIONS: Dict[ProviderName, int] = {}
MAX_MODEL_CACHE_ENTRIES = 24
MAX_MODEL_CACHE_ENTRIES_PER_PROVIDER = 8
MAX_MODEL_FLIGHTS = 12
MAX_MODEL_FLIGHTS_PER_PROVIDER = 4

_CLAUDE_CONTEXT_ENV = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUEST_METHOD",
)
_GEMINI_TRUTHY = {"1", "true", "yes", "on"}


class _ModelFlight:
    """One bounded per-provider refresh shared by concurrent callers."""

    def __init__(self, *, force_refresh: bool) -> None:
        self.done = threading.Event()
        self.result: Optional[Tuple[_ModelRecord, ...]] = None
        self.error: Optional[BaseException] = None
        self.force_refresh = force_refresh


def _fingerprint(fields: Iterable[Tuple[str, object]]) -> _ModelContext:
    """Hash contextual inputs with unambiguous framing; retain no raw values."""

    digest = hashlib.sha256()
    for name, value in fields:
        encoded_name = name.encode("utf-8", "surrogatepass")
        encoded_value = str(value).encode("utf-8", "surrogatepass")
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(encoded_value).to_bytes(8, "big"))
        digest.update(encoded_value)
    return digest.hexdigest()


def _passive_stat_fields(
    prefix: str, path: str, *, follow_symlinks: bool
) -> Tuple[Tuple[str, object], ...]:
    try:
        value = os.stat(path, follow_symlinks=follow_symlinks)
    except OSError:
        return ((prefix + ".exists", 0),)
    return (
        (prefix + ".exists", 1),
        (prefix + ".dev", value.st_dev),
        (prefix + ".ino", value.st_ino),
        (prefix + ".size", value.st_size),
        (prefix + ".mtime_ns", value.st_mtime_ns),
        (prefix + ".ctime_ns", value.st_ctime_ns),
        (prefix + ".mode", value.st_mode),
    )


def _claude_context() -> _ModelContext:
    fields = [
        (
            "anthropic_api_key",
            (os.environ.get("ANTHROPIC_API_KEY") or "").strip(),
        )
    ]
    fields.extend((name, os.environ.get(name, "")) for name in _CLAUDE_CONTEXT_ENV)
    return _fingerprint(fields)


def _codex_context() -> _ModelContext:
    home = os.path.realpath(os.path.abspath(os.path.expanduser("~")))
    cache_path = os.path.realpath(
        os.path.abspath(os.path.join(home, ".codex", "models_cache.json"))
    )
    fields = [("home", home), ("cache_path", cache_path)]
    fields.extend(
        _passive_stat_fields("cache", cache_path, follow_symlinks=True)
    )
    return _fingerprint(fields)


def _gemini_context() -> _ModelContext:
    enabled = (
        os.environ.get("UNIFIED_CLI_ENABLE_GEMINI", "").strip().lower()
        in _GEMINI_TRUTHY
    )
    home = os.path.realpath(os.path.abspath(os.path.expanduser("~")))
    fields = [
        ("enabled", int(enabled)),
        ("home", home),
        ("path", os.environ.get("PATH", "")),
        ("agy_cli_path", os.environ.get("AGY_CLI_PATH", "")),
    ]
    selected = None
    if enabled:
        # Discovery performs only filesystem/PATH checks.  It neither imports
        # provider plugins nor executes the selected binary.
        from .discovery import find_agy_bin

        selected = find_agy_bin()
    if selected is None:
        fields.append(("selected", 0))
    else:
        invoked = os.path.abspath(selected)
        target = os.path.realpath(invoked)
        fields.extend(
            (
                ("selected", 1),
                ("invoked_path", invoked),
                ("target_path", target),
            )
        )
        fields.extend(
            _passive_stat_fields("invoked", invoked, follow_symlinks=False)
        )
        fields.extend(
            _passive_stat_fields("target", target, follow_symlinks=True)
        )
    return _fingerprint(fields)


def _model_context(provider: ProviderName) -> _ModelContext:
    if provider == "claude":
        return _claude_context()
    if provider == "codex":
        return _codex_context()
    return _gemini_context()


def _freeze_models(items: list[ModelInfo]) -> Tuple[_ModelRecord, ...]:
    return tuple(
        (
            item.id,
            item.provider,
            item.display_name,
            bool(item.default),
            bool(item.deprecated),
            item.source,
        )
        for item in items
    )


def _thaw_models(items: Tuple[_ModelRecord, ...]) -> list[ModelInfo]:
    return [
        ModelInfo(
            id=item[0],
            provider=item[1],
            display_name=item[2],
            default=item[3],
            deprecated=item[4],
            source=item[5],  # type: ignore[arg-type]
        )
        for item in items
    ]


def _cached(provider: ProviderName) -> Optional[list[ModelInfo]]:
    context = _model_context(provider)
    key = (provider, context)
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if not entry:
            return None
        expires_at, items = entry
        if now >= expires_at:
            _CACHE.pop(key, None)
            return None
        _CACHE.move_to_end(key)
        return _thaw_models(items)


def _peek_cached_or_hardcoded(provider: ProviderName) -> list[ModelInfo]:
    """Return a passive in-memory snapshot without computing cache context.

    REPL completion setup must not stat provider files, inspect PATH, or run a
    provider finder merely to render candidates.  The newest non-expired cache
    record already resident for this provider is safe to reuse; otherwise the
    static hardcoded order is returned.
    """

    now = time.monotonic()
    with _CACHE_LOCK:
        for key in reversed(_CACHE):
            if key[0] != provider:
                continue
            expires_at, items = _CACHE[key]
            if now < expires_at:
                return _thaw_models(items)
    return _hardcoded(provider)


def _clear_provider_cache_locked(provider: ProviderName) -> None:
    for key in tuple(_CACHE):
        if key[0] == provider:
            _CACHE.pop(key, None)


def _store_cache_locked(
    key: _ModelCacheKey, items: Tuple[_ModelRecord, ...]
) -> None:
    _CACHE[key] = (time.monotonic() + _TTL, items)
    _CACHE.move_to_end(key)
    provider = key[0]
    while sum(1 for candidate in _CACHE if candidate[0] == provider) > (
        MAX_MODEL_CACHE_ENTRIES_PER_PROVIDER
    ):
        evicted = next(candidate for candidate in _CACHE if candidate[0] == provider)
        _CACHE.pop(evicted, None)
    while len(_CACHE) > MAX_MODEL_CACHE_ENTRIES:
        _CACHE.popitem(last=False)


def _busy_error(provider: ProviderName) -> UnifiedError:
    return UnifiedError(
        kind="resource_limit",
        provider=provider,
        message="Model discovery is busy.",
        hint="Wait for an in-flight model refresh to finish, then retry.",
    )


def invalidate_model_cache(provider: Optional[ProviderName] = None) -> None:
    """Invalidate one built-in model cache, or every built-in cache.

    In-flight refreshes still return to their explicit callers, but a refresh
    that started before this invalidation cannot silently repopulate the cache.
    """

    with _CACHE_LOCK:
        providers = tuple(_LISTERS) if provider is None else (provider,)
        for name in providers:
            if name not in _LISTERS:
                raise ValueError("model cache invalidation requires a built-in provider")
            _clear_provider_cache_locked(name)  # type: ignore[arg-type]
            _CACHE_GENERATIONS[name] = _CACHE_GENERATIONS.get(name, 0) + 1  # type: ignore[index]


def _refresh_models(
    provider: ProviderName, *, force_refresh: bool
) -> list[ModelInfo]:
    """Refresh one provider with per-key single-flight coordination."""

    context = _model_context(provider)
    cache_key = (provider, context)
    with _CACHE_LOCK:
        generation = _CACHE_GENERATIONS.get(provider, 0)
        flight_key = (provider, generation, context)
        flight = _CACHE_FLIGHTS.get(flight_key)

        if force_refresh and not (
            flight is not None and flight.force_refresh
        ):
            # A force refresh is a fence: later callers cannot join an older
            # ordinary refresh or consume the previous cached generation.
            generation += 1
            _CACHE_GENERATIONS[provider] = generation
            _clear_provider_cache_locked(provider)
            flight_key = (provider, generation, context)
            flight = _CACHE_FLIGHTS.get(flight_key)

        if not force_refresh:
            entry = _CACHE.get(cache_key)
            if entry is not None:
                expires_at, record = entry
                if time.monotonic() < expires_at:
                    _CACHE.move_to_end(cache_key)
                    return _thaw_models(record)
                _CACHE.pop(cache_key, None)

        if flight is None:
            provider_flights = sum(
                1 for candidate in _CACHE_FLIGHTS if candidate[0] == provider
            )
            if (
                len(_CACHE_FLIGHTS) >= MAX_MODEL_FLIGHTS
                or provider_flights >= MAX_MODEL_FLIGHTS_PER_PROVIDER
            ):
                raise _busy_error(provider)
            flight = _ModelFlight(force_refresh=force_refresh)
            _CACHE_FLIGHTS[flight_key] = flight
            owner = True
        else:
            owner = False

    if not owner:
        flight.done.wait()
        if flight.error is not None:
            raise flight.error
        assert flight.result is not None
        return _thaw_models(flight.result)

    try:
        result = _freeze_models(_LISTERS[provider]())
        current_context = _model_context(provider)
        with _CACHE_LOCK:
            if (
                result
                and _CACHE_GENERATIONS.get(provider, 0) == generation
                and current_context == context
            ):
                _store_cache_locked(cache_key, result)
            flight.result = result
        return _thaw_models(result)
    except BaseException as error:
        with _CACHE_LOCK:
            flight.error = error
        raise
    finally:
        with _CACHE_LOCK:
            if _CACHE_FLIGHTS.get(flight_key) is flight:
                _CACHE_FLIGHTS.pop(flight_key, None)
            flight.done.set()


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
    from .providers.gemini import gemini_enabled

    # The agy/gemini provider is gated off by default for ToS/account-ban
    # reasons. Listing models must NOT spawn `agy` (doctor/status/dashboard/
    # server all call this) unless the user has opted in — otherwise the very
    # subprocess the gate exists to prevent runs anyway.
    if not gemini_enabled():
        return _hardcoded("gemini")

    agy = find_agy_bin()
    if not agy:
        return _hardcoded("gemini")
    try:
        out = subprocess.run(
            [agy, "models"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", input="", timeout=15,
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
    provider: Optional[ProviderId] = None,
    *,
    force_refresh: bool = False,
    extension_launch: Optional["ExtensionLaunchOverridesV1"] = None,
) -> list[ModelInfo]:
    """Return available models for a provider, or all providers combined."""
    if extension_launch is not None and (not provider or provider in _LISTERS):
        raise UnifiedError(
            kind="config",
            provider=provider or "claude",
            message=(
                "Extension launch configuration requires one explicit "
                "extension provider."
            ),
        )
    if provider:
        if provider not in _LISTERS:
            # An extension model listing is always explicit and loads exactly
            # that provider. Extension listers own any cache/refresh policy;
            # core's ``force_refresh`` flag applies only to built-ins.
            from .registry import list_extension_models
            return list_extension_models(
                provider, extension_launch=extension_launch,
            )
        return _refresh_models(  # type: ignore[arg-type]
            provider, force_refresh=force_refresh
        )

    out: list[ModelInfo] = []
    for p in ("claude", "codex", "gemini"):  # type: ignore[assignment]
        out += list_models(p, force_refresh=force_refresh)
    return out
