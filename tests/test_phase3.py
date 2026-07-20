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


def _install_fake_chat(monkeypatch):
    """Capture CLI chat construction without spawning a provider binary."""
    from unified_cli import cli

    seen = {}

    class _FakeResp:
        text = "reply"
        provider = "claude"
        model = "model"
        session_id = "session-1"

        class usage:
            input_tokens = 1
            output_tokens = 1
            total_tokens = 2

    class _FakeClient:
        name = "claude"
        model = "model"

        def chat(self, prompt, **kwargs):
            seen["prompt"] = prompt
            seen["chat_kwargs"] = kwargs
            return _FakeResp()

    def fake_create(provider, **kwargs):
        seen["provider"] = provider
        seen["create_kwargs"] = kwargs
        return _FakeClient()

    monkeypatch.setattr(cli, "create", fake_create)
    monkeypatch.setattr(cli, "save_last_session", lambda **kwargs: seen.update(saved=kwargs))
    return cli, seen


def test_chat_uses_configured_default_provider(monkeypatch):
    cli, seen = _install_fake_chat(monkeypatch)
    monkeypatch.setattr(
        cli.settings, "get", lambda key, default=None: "codex" if key == "default_provider" else default
    )

    assert cli.main(["chat", "hello"]) == 0
    assert seen["provider"] == "codex"
    assert seen["create_kwargs"]["model"] is None


def test_chat_continue_restores_saved_cwd_and_explicit_cwd_wins(monkeypatch, tmp_path):
    from unified_cli.state import SessionState

    cli, seen = _install_fake_chat(monkeypatch)
    saved_dir = tmp_path / "saved"
    explicit_dir = tmp_path / "explicit"
    saved_dir.mkdir()
    explicit_dir.mkdir()
    saved = SessionState("claude", "haiku", "saved-session", cwd=str(saved_dir))
    monkeypatch.setattr(cli, "load_last_session", lambda: saved)

    assert cli.main(["chat", "hello", "--continue"]) == 0
    assert seen["provider"] == "claude"
    assert seen["create_kwargs"]["cwd"] == str(saved_dir.resolve())
    assert seen["saved"]["cwd"] == str(saved_dir.resolve())

    assert cli.main(["chat", "hello", "--continue", "--cwd", str(explicit_dir)]) == 0
    assert seen["create_kwargs"]["cwd"] == str(explicit_dir.resolve())
    assert seen["saved"]["cwd"] == str(explicit_dir.resolve())


def test_chat_does_not_reuse_saved_cwd_when_provider_switches(monkeypatch, tmp_path):
    from unified_cli.state import SessionState

    cli, seen = _install_fake_chat(monkeypatch)
    saved_dir = tmp_path / "saved"
    saved_dir.mkdir()
    monkeypatch.setattr(
        cli, "load_last_session",
        lambda: SessionState("claude", "haiku", "saved-session", cwd=str(saved_dir)),
    )

    assert cli.main(["chat", "hello", "--continue", "-m", "gpt-5.4-mini"]) == 0
    assert seen["provider"] == "codex"
    assert seen["create_kwargs"]["cwd"] != str(saved_dir.resolve())


def test_chat_rejects_invalid_explicit_cwd_before_provider_creation(monkeypatch, tmp_path):
    from unified_cli import cli

    called = {"create": False}
    monkeypatch.setattr(cli, "create", lambda *args, **kwargs: called.update(create=True))
    assert cli.main(["chat", "hello", "--cwd", str(tmp_path / "missing")]) == 2
    assert called["create"] is False


def test_config_default_provider_and_version_fast_paths(monkeypatch, capsys):
    from unified_cli import __version__, cli

    calls = []
    monkeypatch.setattr(cli.settings, "set", lambda key, value: calls.append((key, value)))
    monkeypatch.setattr(cli.settings, "get", lambda key, default=None: "codex")

    assert cli.main(["config", "default-provider", "codex"]) == 0
    assert calls == [("default_provider", "codex")]
    assert cli.main(["config", "default-provider", "--reset"]) == 0
    assert calls[-1] == ("default_provider", None)
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.endswith(f"{__version__}\n")


def test_repl_uses_configured_default_provider(monkeypatch):
    from unified_cli import cli, repl

    seen = {}
    monkeypatch.setattr(cli.settings, "get", lambda key, default=None: "codex")
    monkeypatch.setattr(repl, "run_repl", lambda **kwargs: seen.update(kwargs) or 0)
    assert cli.main(["repl"]) == 0
    assert seen["provider"] == "codex"


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

def _fake_saved(provider="claude", model="claude-opus-4-7", sid="sess-abcdef123456", cwd=""):
    from unified_cli.state import SessionState
    return SessionState(provider=provider, model=model, session_id=sid,
                        cwd=cwd, updated_at=time.time() - 120)


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


def test_cross_provider_context_can_be_disabled_without_blocking_switches():
    from unified_cli.conversation import UnifiedConversation, Turn

    conv = UnifiedConversation(
        default_provider="claude", sticky=False, cross_provider_context=False,
    )
    conv.turns.append(Turn(provider="claude", prompt="private q", text="private a"))
    assert conv._context_prefix_if_switch("codex") == ""


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


def test_apply_resume_restores_valid_cwd_unless_explicit(monkeypatch, tmp_path):
    from unified_cli import repl, state
    from unified_cli.conversation import UnifiedConversation

    saved_dir = tmp_path / "saved"
    explicit_dir = tmp_path / "explicit"
    saved_dir.mkdir()
    explicit_dir.mkdir()
    monkeypatch.setattr(state, "load_last_session", lambda: _fake_saved(cwd=str(saved_dir)))

    conv = UnifiedConversation(default_provider="claude", sticky=False)
    current = {"provider": "claude", "model": "m"}
    opts = {"cwd": str(tmp_path)}
    assert repl._apply_resume(conv, current, opts) is True
    assert opts["cwd"] == str(saved_dir.resolve())

    conv = UnifiedConversation(default_provider="claude", sticky=False)
    current = {"provider": "claude", "model": "m"}
    opts = {"cwd": str(explicit_dir.resolve())}
    assert repl._apply_resume(conv, current, opts, preserve_cwd=True) is True
    assert opts["cwd"] == str(explicit_dir.resolve())


def test_repl_exit_persists_effective_cwd(monkeypatch, tmp_path):
    from unified_cli import repl
    from unified_cli.conversation import UnifiedConversation

    seen = {}
    monkeypatch.setattr(repl, "save_last_session", lambda **kwargs: seen.update(kwargs))
    conv = UnifiedConversation(default_provider="claude", sticky=False)
    conv.sessions["claude"] = "session-1"
    repl._on_exit(conv, {"provider": "claude", "model": "haiku"}, {"cwd": str(tmp_path)})
    assert seen["cwd"] == str(tmp_path)
