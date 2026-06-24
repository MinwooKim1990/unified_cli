"""Tests for the safety gates added in 0.1.1:

  1. The agy/gemini provider is disabled by default and only constructible when
     UNIFIED_CLI_ENABLE_GEMINI is set (ToS / account-ban risk).
  2. The OpenAI-compatible server refuses a non-loopback bind unless
     UNIFIED_CLI_ALLOW_EXTERNAL_BIND is set.

claude / codex are unaffected and must still construct without any opt-in.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unified_cli import UnifiedError, create
from unified_cli.providers.gemini import gemini_enabled


# ---- gemini/agy opt-in gate ----

def test_gemini_disabled_by_default(monkeypatch):
    monkeypatch.delenv("UNIFIED_CLI_ENABLE_GEMINI", raising=False)
    assert gemini_enabled() is False
    with pytest.raises(UnifiedError) as ei:
        create("gemini", bin_path="agy")  # bin_path stub: gate fires before discovery
    err = ei.value
    assert err.kind == "config"
    assert err.provider == "gemini"
    # The message/hint must point the user at the opt-in env var.
    assert "UNIFIED_CLI_ENABLE_GEMINI" in (err.hint or "")


def test_gemini_enabled_with_env(monkeypatch):
    monkeypatch.setenv("UNIFIED_CLI_ENABLE_GEMINI", "1")
    assert gemini_enabled() is True
    cli = create("gemini", bin_path="agy")  # must not raise
    assert cli.name == "gemini"


@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("no", False), ("", False), ("nope", False),
])
def test_gemini_enabled_truthy_matrix(monkeypatch, val, expected):
    monkeypatch.setenv("UNIFIED_CLI_ENABLE_GEMINI", val)
    assert gemini_enabled() is expected


def test_claude_and_codex_not_gated(monkeypatch):
    monkeypatch.delenv("UNIFIED_CLI_ENABLE_GEMINI", raising=False)
    # Should construct with no opt-in (bin_path stub avoids needing the CLI).
    assert create("claude", bin_path="claude").name == "claude"
    assert create("codex", bin_path="codex").name == "codex"


# ---- server localhost bind guard ----

def test_server_refuses_external_bind_without_optin(monkeypatch):
    monkeypatch.delenv("UNIFIED_CLI_ALLOW_EXTERNAL_BIND", raising=False)
    from unified_cli import server
    with pytest.raises(UnifiedError) as ei:
        server.run(host="0.0.0.0", port=8000)  # raises before any uvicorn.run
    assert ei.value.kind == "config"
    assert "UNIFIED_CLI_ALLOW_EXTERNAL_BIND" in (ei.value.hint or "")


def test_server_allows_external_bind_with_optin(monkeypatch):
    monkeypatch.setenv("UNIFIED_CLI_ALLOW_EXTERNAL_BIND", "1")
    captured = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(kw))
    from unified_cli import server
    server.run(host="0.0.0.0", port=1234)
    assert captured.get("host") == "0.0.0.0"
    assert captured.get("port") == 1234


def test_server_loopback_bind_does_not_raise(monkeypatch):
    monkeypatch.delenv("UNIFIED_CLI_ALLOW_EXTERNAL_BIND", raising=False)
    captured = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(kw))
    from unified_cli import server
    server.run(host="127.0.0.1", port=9000)  # loopback → no guard, no real serve
    assert captured.get("host") == "127.0.0.1"
