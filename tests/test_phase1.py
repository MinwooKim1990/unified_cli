"""Regression tests for Phase 1 fixes (flagship verification + safety).

F1: hardcoded list refresh (Claude 4.7 / Codex with gpt-5.5 / Gemini correct IDs)
F2: empty API key env guard
F3: Claude is_error JSON raise
F4: subprocess timeout defaults
F5: state file 0o600 perms
F6: Response.model uses resolved model from CLI response
F7: claude model_not_allowed regex broadened
F9: codex hint dynamically derived from _HARDCODED
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unified_cli.errors import HINTS, classify
from unified_cli.models import _HARDCODED, _list_claude, _list_gemini
from unified_cli.providers.claude import ClaudeProvider
from unified_cli import UnifiedError


# ---- F1: hardcoded list contents ----

def test_f1_claude_hardcoded_includes_flagship_4_7():
    ids = _HARDCODED["claude"]
    assert "claude-opus-4-7" in ids, f"missing Opus 4.7 in {ids}"
    assert "claude-sonnet-4-6" in ids
    assert "claude-haiku-4-5" in ids
    # aliases
    assert "opus" in ids and "sonnet" in ids and "haiku" in ids


def test_f1_codex_hardcoded_includes_flagship_and_codex_specialists():
    ids = _HARDCODED["codex"]
    # gpt-5.5 (flagship, may need newer codex CLI)
    assert "gpt-5.5" in ids
    assert "gpt-5.4" in ids
    assert "gpt-5.4-mini" in ids
    # Coding-specialized (verified live)
    assert "gpt-5.3-codex" in ids
    assert "gpt-5.3-codex-spark" in ids


def test_f1_gemini_hardcoded_includes_both_preview_and_bare_variants():
    ids = _HARDCODED["gemini"]
    # Both -preview and bare variants are listed so users can try whichever
    # their subscription resolves to (see PHASE0_VERIFICATION.md).
    assert "gemini-3.1-pro-preview" in ids
    assert "gemini-3.1-pro" in ids                # bare form also included
    assert "gemini-3-flash-preview" in ids
    assert "gemini-3.1-flash" in ids
    assert "gemini-3.1-flash-lite-preview" in ids
    assert "gemini-3.1-flash-lite" in ids


# ---- F2: empty API key env guards ----

def test_f2_empty_anthropic_key_falls_back_to_hardcoded():
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False):
        models = _list_claude()
    assert all(m.source == "hardcoded" for m in models)


def test_f2_whitespace_only_anthropic_key_falls_back():
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "   \n\t  "}, clear=False):
        models = _list_claude()
    assert all(m.source == "hardcoded" for m in models)


def test_f2_empty_gemini_key_falls_back():
    env = {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""}
    with patch.dict(os.environ, env, clear=False):
        models = _list_gemini()
    assert all(m.source == "hardcoded" for m in models)


# ---- F3: Claude is_error raise ----

def test_f3_claude_is_error_raises():
    fake = json.dumps({
        "is_error": True,
        "result": "model 'claude-bogus-9-9' not found",
    })
    cli = ClaudeProvider.__new__(ClaudeProvider)  # bypass __init__
    try:
        cli._parse_json_response(fake, "opus")
    except UnifiedError as e:
        assert e.kind == "internal"
        assert "claude-bogus-9-9" in e.message
    else:
        assert False, "should have raised"


def test_f3_claude_normal_response_passes_through():
    fake = json.dumps({
        "result": "hello",
        "session_id": "abc",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    })
    cli = ClaudeProvider.__new__(ClaudeProvider)
    resp = cli._parse_json_response(fake, "opus")
    assert resp.text == "hello"
    assert resp.session_id == "abc"


# ---- F6: resolved model surfaces ----

def test_f6_resolved_model_from_modelusage():
    fake = json.dumps({
        "result": "ok",
        "session_id": "s1",
        "usage": {"input_tokens": 5, "output_tokens": 2},
        "modelUsage": {"claude-opus-4-7": {"input": 5, "output": 2}},
    })
    cli = ClaudeProvider.__new__(ClaudeProvider)
    resp = cli._parse_json_response(fake, "opus")
    # User passed alias "opus", but Response.model exposes the actual snapshot
    assert resp.model == "claude-opus-4-7", \
        f"expected resolved model, got {resp.model}"


def test_f6_falls_back_to_requested_when_no_modelusage():
    fake = json.dumps({"result": "ok", "session_id": "s1"})
    cli = ClaudeProvider.__new__(ClaudeProvider)
    resp = cli._parse_json_response(fake, "claude-haiku-4-5")
    assert resp.model == "claude-haiku-4-5"


# ---- F7: broader Claude model_not_allowed regex ----

def test_f7_is_not_a_valid_model_classified():
    err = classify("claude", stderr="Error: 'foo' is not a valid model identifier")
    assert err.kind == "model_not_allowed"


def test_f7_requested_model_is_not_available_classified():
    err = classify("claude", stderr="requested model is not available for your account")
    assert err.kind == "model_not_allowed"


def test_f7_existing_not_exist_pattern_still_works():
    err = classify("claude", stderr="model 'xyz' does not exist or not accessible")
    assert err.kind == "model_not_allowed"


# ---- F9: codex hint dynamically composed ----

def test_f9_codex_hint_includes_subscription_models():
    hint = HINTS["codex_subscription_models"]
    # Should include at least one model from _HARDCODED["codex"]
    assert "gpt-5.4" in hint or "gpt-5.5" in hint
    # Should mention the upgrade path for users blocked on gpt-5.5
    assert "upgrade" in hint or "업그레이드" in hint or "newer" in hint


def test_f9_codex_hint_contains_actual_hardcoded_models():
    hint = HINTS["codex_subscription_models"]
    # All non-codex-prefixed slugs from _HARDCODED should appear
    for m in _HARDCODED["codex"]:
        if not m.startswith("codex-"):
            assert m in hint, f"hint missing {m}"


# ---- F5: state file permissions (smoke test) ----

def test_f5_state_file_perm_is_0600(tmp_path):
    import unified_cli.state as state_mod
    tmp_dir = tmp_path / ".unified-cli"
    with patch.multiple(
        state_mod,
        STATE_DIR=tmp_dir,
        STATE_FILE=tmp_dir / "state.json",
    ):
        state_mod.save_last_session("claude", "haiku", "abc-123")
        mode = stat.S_IMODE(state_mod.STATE_FILE.stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


# ---- F4: subprocess timeout default present ----

def test_f4_default_timeouts_set():
    from unified_cli.base import DEFAULT_CHAT_TIMEOUT, DEFAULT_STREAM_TIMEOUT
    assert DEFAULT_CHAT_TIMEOUT >= 60       # not infinite
    assert DEFAULT_STREAM_TIMEOUT > DEFAULT_CHAT_TIMEOUT


if __name__ == "__main__":
    import traceback, tempfile, shutil, inspect
    passed = failed = 0
    for name, fn in list(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        try:
            sig = inspect.signature(fn)
            if "tmp_path" in sig.parameters:
                tmp = Path(tempfile.mkdtemp())
                try:
                    fn(tmp)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
            else:
                fn()
            passed += 1
            print(f"  ✓ {name}")
        except Exception:
            failed += 1
            print(f"  ✗ {name}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
