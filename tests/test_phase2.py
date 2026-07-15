"""Phase 2 (security + correctness) regression tests:
server localhost guard + bounded CONVS, setup gemini gate, conversation
early-stop session persistence, readline perms, argv `--` sentinel.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unified_cli.core import Message


# ---- server: localhost guard (#7) ----

def test_host_header_name_parsing():
    from unified_cli import server
    assert server._host_header_name("127.0.0.1:8000") == "127.0.0.1"
    assert server._host_header_name("[::1]:8000") == "::1"
    assert server._host_header_name("localhost") == "localhost"
    assert server._host_header_name("evil.com:80") == "evil.com"


def test_is_loopback_host():
    from unified_cli import server
    assert server._is_loopback_host("127.0.0.1")
    assert server._is_loopback_host("127.5.5.5")
    assert server._is_loopback_host("::1")
    assert server._is_loopback_host("localhost")
    assert server._is_loopback_host("LOCALHOST")  # case-insensitive
    assert not server._is_loopback_host("10.0.0.9")
    assert not server._is_loopback_host("evil.com")


def test_is_loopback_host_rejects_dns_rebinding():
    # A string prefix "127." would wrongly accept these attacker-controlled DNS
    # names; the ipaddress-based check must reject anything that isn't a real
    # loopback IP or the literal "localhost".
    from unified_cli import server
    assert not server._is_loopback_host("127.evil.com")
    assert not server._is_loopback_host("127.0.0.1.evil.com")
    assert not server._is_loopback_host("127.0.0.1.")   # trailing-dot FQDN
    assert not server._is_loopback_host("0.0.0.0")
    assert not server._is_loopback_host("localhost.evil.com")
    # NB: the IPv4-mapped IPv6 literal "::ffff:127.0.0.1" is deliberately NOT
    # asserted — `ipaddress`'s is_loopback delegates to the mapped IPv4 only on
    # Python 3.13+, so it's True there and False on 3.9. Both are safe (it's a
    # genuine loopback literal; rejecting it is merely stricter), so we don't
    # pin the version-dependent stdlib behavior here.


def test_conversations_endpoint_concurrent_mutation():
    # _get_conv mutates the OrderedDict (move_to_end/popitem) on the threadpool
    # while conversations_endpoint iterates it — must not raise "OrderedDict
    # mutated during iteration".
    import threading
    from unified_cli import server
    server.CONVS.clear()
    errors: list = []
    stop = threading.Event()

    def churn():
        i = 0
        while not stop.is_set():
            server._get_conv(f"c-{i % 50}")
            i += 1

    def iterate():
        try:
            for _ in range(2000):
                server.conversations_endpoint()
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))
        finally:
            stop.set()

    w = threading.Thread(target=churn)
    w.start()
    iterate()
    w.join(timeout=5)
    server.CONVS.clear()
    assert not errors, errors


def test_guard_blocks_nonloopback_client_by_default(monkeypatch):
    # TestClient's peer host is "testclient" (non-loopback); with no opt-in the
    # middleware must 403 — proving the guard holds even under a raw uvicorn bind.
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from unified_cli import server
    monkeypatch.delenv("UNIFIED_CLI_ALLOW_EXTERNAL_BIND", raising=False)
    monkeypatch.delenv("UNIFIED_CLI_SERVER_AUTH_TOKEN", raising=False)
    client = TestClient(server.app)
    r = client.get("/healthz")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "config"


def test_guard_allows_when_opted_in(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from unified_cli import server
    monkeypatch.setenv("UNIFIED_CLI_ALLOW_EXTERNAL_BIND", "1")
    token = "test-server-auth-token-at-least-32-bytes"
    monkeypatch.setenv("UNIFIED_CLI_SERVER_AUTH_TOKEN", token)
    client = TestClient(server.app, headers={"Authorization": f"Bearer {token}"})
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ---- server: bounded CONVS (#11) ----

def test_get_conv_lru_bounded(monkeypatch):
    from unified_cli import server
    server.CONVS.clear()
    monkeypatch.setattr(server, "_MAX_CONVS", 5)
    for i in range(20):
        server._get_conv(f"id-{i}")
    assert len(server.CONVS) == 5
    # Oldest evicted, newest kept.
    assert "id-0" not in server.CONVS
    assert "id-19" in server.CONVS
    server.CONVS.clear()


def test_get_conv_move_to_end_on_access(monkeypatch):
    from unified_cli import server
    server.CONVS.clear()
    monkeypatch.setattr(server, "_MAX_CONVS", 3)
    server._get_conv("a")
    server._get_conv("b")
    server._get_conv("c")
    server._get_conv("a")   # touch → most-recent
    server._get_conv("d")   # evicts LRU, which is now "b" (not "a")
    assert "a" in server.CONVS
    assert "b" not in server.CONVS
    server.CONVS.clear()


# ---- setup gemini gate (#15) ----

def _fake_states():
    from unified_cli.ui import ProviderState
    mk = lambda n: ProviderState(  # noqa: E731
        name=n, bin_path="/x", has_oauth=False, has_api_key=False,
        api_key_env="X", model_count=1, model_source="hardcoded", default_model="m",
    )
    return [mk("claude"), mk("codex"), mk("gemini")]


def test_setup_skips_gemini_when_gated(monkeypatch):
    from unified_cli import onboarding
    monkeypatch.delenv("UNIFIED_CLI_ENABLE_GEMINI", raising=False)
    monkeypatch.setattr(onboarding, "collect_states", _fake_states)
    logged: list[str] = []
    verified: list[str] = []
    monkeypatch.setattr(onboarding, "_login_one",
                        lambda s, c: (logged.append(s.name), _sr(s.name))[1])
    monkeypatch.setattr(onboarding, "_verify_all",
                        lambda states, c: (verified.extend(s.name for s in states), [])[1])
    onboarding.run_setup(skip_install=True)
    assert "gemini" not in logged     # never spawns agy login
    assert "gemini" not in verified   # never verified behind the gate


def test_setup_includes_gemini_when_enabled(monkeypatch):
    from unified_cli import onboarding
    monkeypatch.setenv("UNIFIED_CLI_ENABLE_GEMINI", "1")
    monkeypatch.setattr(onboarding, "collect_states", _fake_states)
    verified: list[str] = []
    monkeypatch.setattr(onboarding, "_login_one", lambda s, c: _sr(s.name))
    monkeypatch.setattr(onboarding, "_verify_all",
                        lambda states, c: verified.extend(s.name for s in states) or [])
    onboarding.run_setup(skip_install=True)
    assert "gemini" in verified


def _sr(name):
    from unified_cli.onboarding import StepResult
    return StepResult(name, True, "")


# ---- conversation: early-stop session persistence (#17) ----

class _FakeClient:
    name = "claude"
    model = "m"

    def stream(self, prompt, *, session_id=None, images=None):
        yield Message(kind="session", provider="claude", session_id="sess-xyz")
        yield Message(kind="text", provider="claude", text="hello")
        yield Message(kind="text", provider="claude", text=" world")


def test_stream_persists_session_on_early_stop(monkeypatch):
    from unified_cli.conversation import UnifiedConversation
    conv = UnifiedConversation(default_provider="claude", sticky=False)
    monkeypatch.setattr(conv, "_get_client", lambda p, m: _FakeClient())
    gen = conv.stream("hi", provider="claude")
    assert next(gen).kind == "session"
    assert next(gen).text == "hello"
    gen.close()  # consumer abort mid-stream → GeneratorExit
    assert conv.sessions.get("claude") == "sess-xyz"  # session not lost
    assert len(conv.turns) == 1                        # partial turn recorded
    assert conv.turns[0].text == "hello"


def test_stream_full_consumption_records_full_turn(monkeypatch):
    from unified_cli.conversation import UnifiedConversation
    conv = UnifiedConversation(default_provider="claude", sticky=False)
    monkeypatch.setattr(conv, "_get_client", lambda p, m: _FakeClient())
    out = [m for m in conv.stream("hi", provider="claude") if m.kind == "text"]
    assert "".join(m.text for m in out) == "hello world"
    assert conv.turns[0].text == "hello world"
    assert conv.sessions["claude"] == "sess-xyz"


# ---- readline history perms (#19) ----

def test_readline_history_is_owner_only(tmp_path, monkeypatch):
    from unified_cli import repl
    hist = tmp_path / ".unified-cli" / "repl_history"
    monkeypatch.setattr(repl, "_HISTORY_FILE", hist)
    repl._setup_readline()
    if not hist.exists():
        pytest.skip("readline unavailable")
    assert stat.S_IMODE(os.stat(hist).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(hist.parent).st_mode) == 0o700


# ---- argv `--` sentinel (#20) ----

def test_claude_dash_prompt_gets_sentinel():
    from unified_cli.providers.claude import ClaudeProvider
    p = ClaudeProvider(bin_path="claude")
    args, _ = p._build_args("--version", session_id=None, resume_last=False,
                            model=None, streaming=False)
    assert "--" in args
    assert args.index("--") < args.index("--version")


def test_claude_normal_prompt_no_sentinel():
    # web_search=False so no --allowedTools is emitted; only then is the
    # sentinel unnecessary (the default web_search=True adds WebSearch/WebFetch
    # to --allowedTools, which is variadic and requires the sentinel).
    from unified_cli.providers.claude import ClaudeProvider
    p = ClaudeProvider(bin_path="claude", web_search=False)
    args, _ = p._build_args("hello there", session_id=None, resume_last=False,
                            model=None, streaming=False)
    assert "--" not in args


def test_claude_web_search_gets_sentinel():
    # --allowedTools is variadic (<tools...>): without a "--" sentinel it
    # swallows the positional prompt and the CLI exits 1 with "Input must be
    # provided either through stdin or as a prompt argument".
    from unified_cli.providers.claude import ClaudeProvider
    p = ClaudeProvider(bin_path="claude", web_search=True)
    args, _ = p._build_args("hello there", session_id=None, resume_last=False,
                            model=None, streaming=True)
    assert args[-1] == "hello there"
    assert args[-2] == "--"
    assert args.index("--allowedTools") < args.index("--")


def test_claude_disallowed_tools_get_sentinel():
    from unified_cli.providers.claude import ClaudeProvider
    p = ClaudeProvider(bin_path="claude", disallowed_tools=["Bash"])
    args, _ = p._build_args("hello there", session_id=None, resume_last=False,
                            model=None, streaming=False)
    assert args[-1] == "hello there"
    assert args[-2] == "--"


def test_claude_add_dirs_get_sentinel():
    from unified_cli.providers.claude import ClaudeProvider
    p = ClaudeProvider(bin_path="claude", web_search=False, add_dirs=["/workspace"])
    args, _ = p._build_args("hello there", session_id=None, resume_last=False,
                            model=None, streaming=False)
    assert args[-4:] == ["--add-dir", "/workspace", "--", "hello there"]


def test_codex_dash_prompt_gets_sentinel():
    from unified_cli.providers.codex import CodexProvider
    p = CodexProvider(bin_path="codex")
    args, _ = p._build_args("--help", session_id=None, resume_last=False,
                            model=None, streaming=False)
    assert "--" in args
    assert args.index("--") < args.index("--help")
