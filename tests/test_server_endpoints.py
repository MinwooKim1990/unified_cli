"""HTTP-layer tests for the OpenAI-compatible server (#16).

Uses starlette's TestClient with the localhost guard opted out and the provider
layer stubbed, so nothing shells out to a real CLI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from unified_cli import server  # noqa: E402
from unified_cli.core import Message, Response, Usage  # noqa: E402
from unified_cli.errors import UnifiedError  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    # Opt out of the localhost guard (TestClient's peer is non-loopback) and
    # start each test with a clean conversation store.
    monkeypatch.setenv("UNIFIED_CLI_ALLOW_EXTERNAL_BIND", "1")
    monkeypatch.delenv("UNIFIED_CLI_ENABLE_GEMINI", raising=False)
    server.CONVS.clear()
    return TestClient(server.app)


def _fake_response(provider="claude", model="m"):
    return Response(text="hi there", session_id="s1", provider=provider,
                    model=model, usage=Usage(input_tokens=3, output_tokens=2),
                    messages=[], raw=[])


def test_chat_completion_openai_shape(client, monkeypatch):
    monkeypatch.setattr(server.UnifiedConversation, "send",
                        lambda self, prompt, **kw: _fake_response(model=kw.get("model") or "m"))
    r = client.post("/v1/chat/completions", json={
        "model": "haiku",
        "messages": [{"role": "user", "content": "hi"}],
        "user": "t1",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "hi there"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["usage"]["prompt_tokens"] == 3
    assert body["usage"]["completion_tokens"] == 2
    assert body["x_conversation_id"] == "t1"


def test_chat_completion_rate_limit_maps_to_429(client, monkeypatch):
    def boom(self, prompt, **kw):
        raise UnifiedError(kind="rate_limit", provider="claude",
                           message="slow down", hint="wait")
    monkeypatch.setattr(server.UnifiedConversation, "send", boom)
    r = client.post("/v1/chat/completions", json={
        "model": "haiku", "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 429
    assert r.json()["detail"]["type"] == "rate_limit_error"


def test_chat_completion_streaming_sse_terminates(client, monkeypatch):
    def fake_stream(self, prompt, **kw):
        yield Message(kind="session", provider="claude", session_id="s1")
        yield Message(kind="text", provider="claude", text="hello ")
        yield Message(kind="text", provider="claude", text="world")
    monkeypatch.setattr(server.UnifiedConversation, "stream", fake_stream)
    r = client.post("/v1/chat/completions", json={
        "model": "haiku", "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    })
    assert r.status_code == 200
    text = r.text
    assert "hello " in text and "world" in text
    assert "data: [DONE]" in text
    assert '"finish_reason": "stop"' in text


def test_empty_messages_400(client):
    r = client.post("/v1/chat/completions", json={"model": "haiku", "messages": []})
    assert r.status_code == 400


def test_gemini_model_gated_returns_4xx_without_spawning_agy(client, monkeypatch):
    # Patch the binding the code path ACTUALLY calls: GeminiProvider._discover_bin
    # → find_agy_bin, imported into the gemini module namespace. If the gate were
    # removed, construction would reach _discover_bin and bump this counter.
    import unified_cli.providers.gemini as gem
    calls = {"n": 0}
    monkeypatch.setattr(gem, "find_agy_bin",
                        lambda: calls.__setitem__("n", calls["n"] + 1) or "/fake/agy")
    r = client.post("/v1/chat/completions", json={
        "model": "gemini-3.5-flash",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code in (400, 401, 403)   # gate → config error
    assert calls["n"] == 0                     # gate blocks BEFORE binary discovery


def test_models_endpoint_ok(client):
    r = client.get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list) and body["data"]


def test_doctor_endpoint_has_new_fields(client):
    r = client.get("/v1/doctor")
    assert r.status_code == 200
    row = r.json()[0]
    assert "keychain" in row and "has_token_env" in row


def test_root_redirects_to_dashboard(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (307, 308)
    assert r.headers["location"] == "/dashboard"


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}
