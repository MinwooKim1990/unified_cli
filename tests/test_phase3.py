"""Phase 3 (UX) regression tests: lazy-import startup, chat pipe/stdin UX,
serve subcommand wiring, and REPL session resume.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

_SRC = str(Path(__file__).resolve().parents[1] / "src")


# ---- #18 lazy imports: importing the CLI must not pull prompt_toolkit ----

def test_cli_import_does_not_load_prompt_toolkit():
    code = (
        "import sys; sys.path.insert(0, %r); import unified_cli.cli as c;"
        "assert 'prompt_toolkit' not in sys.modules, 'prompt_toolkit eagerly imported';"
        "assert 'unified_cli.onboarding' not in sys.modules, 'onboarding eagerly imported';"
        "print('ok')" % _SRC
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


# ---- #13 chat: no prompt on an interactive TTY exits instead of blocking ----

def test_chat_without_prompt_on_tty_exits(monkeypatch):
    from unified_cli import cli

    class _FakeClient:
        name = "claude"
        model = "m"

    monkeypatch.setattr(cli, "create", lambda *a, **k: _FakeClient())
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    rc = cli.main(["chat"])   # no prompt, "interactive" stdin → must not hang
    assert rc == 2


def test_chat_reads_piped_stdin(monkeypatch):
    # Non-TTY stdin (a pipe) should be read as the prompt, not rejected.
    import io
    from unified_cli import cli

    seen = {}

    class _FakeResp:
        text = "reply"
        provider = "claude"
        model = "m"
        session_id = ""

        class usage:
            input_tokens = 0
            output_tokens = 0
            total_tokens = 0

    class _FakeClient:
        name = "claude"
        model = "m"

        def chat(self, prompt, **kw):
            seen["prompt"] = prompt
            return _FakeResp()

    monkeypatch.setattr(cli, "create", lambda *a, **k: _FakeClient())
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("piped question"))
    # StringIO.isatty() → False, so the prompt comes from stdin.
    rc = cli.main(["chat"])
    assert rc == 0
    assert seen["prompt"] == "piped question"


# ---- #14 serve subcommand wiring ----

def test_serve_invokes_run_on_loopback(monkeypatch):
    from unified_cli import cli, server
    called = {}
    monkeypatch.setattr(server, "run",
                        lambda **kw: called.update(kw))
    import argparse
    ns = argparse.Namespace(port=8123, open=False)
    rc = cli._cmd_serve(ns)
    assert rc == 0
    assert called == {"host": "127.0.0.1", "port": 8123}


# ---- #12 REPL session resume ----

def _fake_saved(provider="claude", model="claude-opus-4-7", sid="sess-abcdef123456"):
    from unified_cli.state import SessionState
    return SessionState(provider=provider, model=model, session_id=sid,
                        cwd="", updated_at=time.time() - 120)


def test_apply_resume_seeds_session(monkeypatch):
    from unified_cli import repl, state
    from unified_cli.conversation import UnifiedConversation
    monkeypatch.setattr(state, "load_last_session", lambda: _fake_saved())
    conv = UnifiedConversation(default_provider="claude", sticky=False)
    current = {"provider": "claude", "model": "claude-haiku-4-5"}
    ok = repl._apply_resume(conv, current)
    assert ok is True
    assert current["provider"] == "claude"
    assert current["model"] == "claude-opus-4-7"
    assert conv.sessions["claude"] == "sess-abcdef123456"
    # Placeholder turn lets _use_native_session engage on the first real turn.
    assert len(conv.turns) == 1
    assert conv._use_native_session("claude") == "sess-abcdef123456"


def test_apply_resume_none(monkeypatch):
    from unified_cli import repl, state
    from unified_cli.conversation import UnifiedConversation
    monkeypatch.setattr(state, "load_last_session", lambda: None)
    conv = UnifiedConversation(default_provider="claude", sticky=False)
    current = {"provider": "claude", "model": "m"}
    assert repl._apply_resume(conv, current) is False
    assert conv.turns == []


def test_context_prefix_skips_empty_placeholder_turn():
    # A resume seed (empty placeholder Turn) must NOT leak a blank
    # "User:/Assistant:" pair into the prompt when the user switches provider.
    from unified_cli.conversation import UnifiedConversation, Turn
    conv = UnifiedConversation(default_provider="claude", sticky=False)
    conv.turns.append(Turn(provider="claude", prompt="", text=""))   # placeholder
    assert conv._context_prefix_if_switch("codex") == ""
    conv.turns.append(Turn(provider="claude", prompt="real q", text="real a"))
    prefix = conv._context_prefix_if_switch("codex")
    assert "real q" in prefix and "real a" in prefix                 # real turn kept


def test_apply_resume_gemini_gated(monkeypatch):
    from unified_cli import repl, state
    from unified_cli.conversation import UnifiedConversation
    monkeypatch.delenv("UNIFIED_CLI_ENABLE_GEMINI", raising=False)
    monkeypatch.setattr(state, "load_last_session",
                        lambda: _fake_saved(provider="gemini", model="gemini-3.5-flash"))
    conv = UnifiedConversation(default_provider="claude", sticky=False)
    current = {"provider": "claude", "model": "m"}
    # Must refuse to resume into the gated provider.
    assert repl._apply_resume(conv, current) is False
    assert current["provider"] == "claude"
    assert conv.turns == []
