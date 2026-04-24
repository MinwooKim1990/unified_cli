"""Unit tests for error classification (stdlib only, no network)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unified_cli.errors import classify


def test_claude_auth_expired():
    err = classify("claude", stderr="API Error: 401 OAuth token has expired", exitcode=1)
    assert err.kind == "auth_expired"
    assert err.provider == "claude"
    assert "claude /login" in err.hint or "ANTHROPIC_API_KEY" in err.hint


def test_claude_rate_limit():
    err = classify("claude", stderr="API Error: 429 rate_limit_error", exitcode=1)
    assert err.kind == "rate_limit"


def test_claude_model_not_allowed():
    err = classify("claude", stderr="model 'claude-xyz' does not exist or not accessible")
    assert err.kind == "model_not_allowed"


def test_codex_model_not_allowed_subscription():
    err = classify(
        "codex",
        stdout='{"error":{"message":"The \'gpt-5\' model is not supported when using Codex with a ChatGPT account"}}',
    )
    assert err.kind == "model_not_allowed"
    assert "gpt-5.4-mini" in err.hint


def test_codex_auth_expired():
    err = classify("codex", stderr='{"type":"authentication_error"}', exitcode=1)
    assert err.kind == "auth_expired"


def test_codex_rate_limit():
    err = classify("codex", stderr="429 Too Many Requests")
    assert err.kind == "rate_limit"


def test_gemini_auth_expired():
    err = classify("gemini", stderr="Error: No refresh token is set", exitcode=1)
    assert err.kind == "auth_expired"


def test_gemini_not_found():
    err = classify(
        "gemini",
        stdout='{"error":{"message":"Requested entity was not found."}}',
    )
    assert err.kind == "not_found"


def test_gemini_rate_limit():
    err = classify("gemini", stderr="RESOURCE_EXHAUSTED Quota exceeded")
    assert err.kind == "rate_limit"


def test_gemini_404_model():
    err = classify("gemini", stderr="404 model 'gemini-xyz' not found")
    assert err.kind == "model_not_allowed"


def test_network_retryable():
    err = classify("claude", stderr="ECONNRESET connection aborted")
    assert err.kind == "network"


def test_unknown_falls_back_to_internal():
    err = classify("claude", stderr="some totally unexpected error", exitcode=77)
    assert err.kind == "internal"
    assert err.cause == "some totally unexpected error"


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
