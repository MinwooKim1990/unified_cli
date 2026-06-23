"""Regression tests for the audit fixes (F1–F4 + U3).

UI fixes (U1/U2/U4) are exercised via manual smoke and not unit-tested
because they depend on argparse/rich output formatting.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unified_cli.base import _check_session_match, _reject_empty_prompt
from unified_cli.errors import UnifiedError, classify


# ---- F1: session_id mismatch detection ----

def test_f1_session_match_passes():
    # both None → no check
    _check_session_match("codex", None, None)
    # only one side → no check (first turn case)
    _check_session_match("codex", None, "abc")
    _check_session_match("codex", "abc", None)
    # equal → no check
    _check_session_match("codex", "abc", "abc")


def test_f1_session_mismatch_raises_not_found():
    try:
        _check_session_match("codex", "aaaaaaaa-0000", "bbbbbbbb-0000")
    except UnifiedError as e:
        assert e.kind == "not_found"
        assert e.provider == "codex"
        assert "aaaaaaaa" in e.message or "aaaaaaaa" in e.cause
    else:
        assert False, "should have raised UnifiedError"


# ---- F2: empty prompt guard ----

def test_f2_empty_prompt_raises_config():
    for bad in ("", "   ", "\n\t", "  \n  "):
        try:
            _reject_empty_prompt(bad, "claude")
        except UnifiedError as e:
            assert e.kind == "config"
            assert "비어" in e.message or "빈" in e.message
        else:
            assert False, f"empty prompt {bad!r} should have raised"


def test_f2_non_empty_prompt_passes():
    _reject_empty_prompt("hi", "claude")
    _reject_empty_prompt("   hello   ", "gemini")  # whitespace ok if content exists


# ---- F3: Claude session not-found classifier ----

def test_f3_claude_session_not_found_variants():
    for stderr in (
        "Error: session 'abc' not found in local state",
        "Session does not exist: abc-123",
        "could not find session abc",
        "unknown session: xyz",
        # Actual Claude 2.1.111 message (probed live):
        "No conversation found with session ID: 00000000-0000-0000-0000-000000000000",
    ):
        err = classify("claude", stderr=stderr)
        assert err.kind == "not_found", f"stderr={stderr!r} → {err.kind}"


def test_f3_claude_model_error_still_classified_as_model_not_allowed():
    # Make sure the new session matcher didn't accidentally catch model errors.
    err = classify("claude", stderr="model 'xyz' does not exist or not accessible")
    assert err.kind == "model_not_allowed"


# ---- F4: Gemini "Requested entity" → model_not_allowed ----

def test_f4_gemini_requested_entity_reclassified():
    err = classify(
        "gemini",
        stdout='{"error":{"message":"Requested entity was not found."}}',
    )
    assert err.kind == "model_not_allowed"


def test_f4_gemini_404_still_model_not_allowed():
    err = classify("gemini", stderr="404 model 'fake' not found")
    assert err.kind == "model_not_allowed"


# ---- U3: Claude terse flag injects system prompt ----

def test_u3_terse_injects_append_system_prompt():
    # Can't instantiate Claude provider without binary, but can inspect the
    # class rule itself.
    from unified_cli.providers.claude import ClaudeProvider
    assert "간결" in ClaudeProvider._TERSE_RULE


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
