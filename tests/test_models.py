"""Unit tests for model listing (uses local cache; no network mandatory)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unified_cli.factory import route
from unified_cli.models import DEFAULT_MODELS, list_models


def test_defaults_marked():
    for provider in ("claude", "codex", "gemini"):
        mods = list_models(provider)  # type: ignore[arg-type]
        defaults = [m for m in mods if m.default]
        assert len(defaults) >= 1, f"{provider}: no default model marked"
        assert defaults[0].id == DEFAULT_MODELS[provider]


def test_all_providers_have_models():
    for provider in ("claude", "codex", "gemini"):
        mods = list_models(provider)  # type: ignore[arg-type]
        assert len(mods) > 0


def test_codex_cache_source_when_file_present():
    # If ~/.codex/models_cache.json exists, source should be "cache" (not hardcoded)
    cache = Path("~/.codex/models_cache.json").expanduser()
    if cache.exists():
        mods = list_models("codex", force_refresh=True)
        sources = {m.source for m in mods}
        assert "cache" in sources, f"expected cache source, got {sources}"


def test_route_claude_aliases():
    assert route("haiku") == ("claude", "haiku")
    assert route("sonnet") == ("claude", "sonnet")
    assert route("claude-haiku-4-5") == ("claude", "claude-haiku-4-5")


def test_route_codex_patterns():
    assert route("gpt-5.4-mini") == ("codex", "gpt-5.4-mini")
    assert route("o1-mini") == ("codex", "o1-mini")
    assert route("o3-pro") == ("codex", "o3-pro")


def test_route_gemini_pattern():
    assert route("gemini-3.1-flash-lite-preview") == (
        "gemini", "gemini-3.1-flash-lite-preview"
    )


def test_route_explicit_prefix_takes_priority():
    assert route("claude/gpt-5")[0] == "claude"
    assert route("codex/haiku")[0] == "codex"


def test_route_unknown_raises():
    from unified_cli.errors import UnifiedError
    try:
        route("random-model-xyz")
        assert False, "should have raised"
    except UnifiedError as e:
        assert e.kind == "config"


if __name__ == "__main__":  # manual run
    import traceback
    passed = failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"  ✓ {name}")
            except Exception:
                failed += 1
                print(f"  ✗ {name}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
