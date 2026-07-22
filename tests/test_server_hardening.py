"""Focused HTTP-boundary and conversation-lease regression tests."""

from __future__ import annotations

import asyncio
import gc
import json
import threading
from pathlib import Path

import pytest

sys_path = str(Path(__file__).resolve().parents[1] / "src")
import sys
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from unified_cli import server
from unified_cli.conversation import UnifiedConversation
from unified_cli.core import Message, ModelInfo, Response, Usage
from unified_cli.errors import UnifiedError


_TEST_SERVER_AUTH_TOKEN = "test-server-token-0123456789-abcdefghijklmnopqrstuvwxyz"


def _response(text: str = "ok") -> Response:
    return Response(
        text=text, session_id="session-1", provider="claude", model="haiku",
        usage=Usage(input_tokens=1, output_tokens=1), messages=[], raw=[],
    )


@pytest.fixture(autouse=True)
def _clean_server(monkeypatch):
    monkeypatch.setenv("UNIFIED_CLI_ALLOW_EXTERNAL_BIND", "1")
    monkeypatch.setenv("UNIFIED_CLI_SERVER_AUTH_TOKEN", _TEST_SERVER_AUTH_TOKEN)
    server._release_all_conversation_leases()
    with server._CONVS_LOCK:
        server.CONVS.clear()
    yield
    server._release_all_conversation_leases()
    with server._CONVS_LOCK:
        server.CONVS.clear()


@pytest.fixture
def client():
    test_client = TestClient(server.app)
    test_client.headers["Authorization"] = f"Bearer {_TEST_SERVER_AUTH_TOKEN}"
    return test_client


@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token-0123456789-abcdefghijklmnopqrstuvwxyz"])
def test_external_chat_requires_valid_bearer_before_provider_invocation(
    monkeypatch, authorization,
):
    calls = {"send": 0}

    def fake_send(self, prompt, **kwargs):
        calls["send"] += 1
        return _response()

    monkeypatch.setattr(server.UnifiedConversation, "send", fake_send)
    bare_client = TestClient(server.app)
    headers = {"Authorization": authorization} if authorization else None
    response = bare_client.post("/v1/chat/completions", headers=headers, json={
        "model": "haiku",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "server_auth_required"
    assert calls["send"] == 0


def test_external_diagnostics_require_bearer():
    bare_client = TestClient(server.app)
    for path in ("/v1/doctor", "/v1/usage", "/v1/conversations", "/dashboard"):
        response = bare_client.get(path)
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "server_auth_required"


def test_external_valid_bearer_allows_chat(client, monkeypatch):
    calls = {"send": 0}

    def fake_send(self, prompt, **kwargs):
        calls["send"] += 1
        return _response()

    monkeypatch.setattr(server.UnifiedConversation, "send", fake_send)
    response = client.post("/v1/chat/completions", json={
        "model": "haiku",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert response.status_code == 200
    assert calls["send"] == 1


def test_raw_external_request_fails_closed_without_strong_token(monkeypatch):
    monkeypatch.delenv("UNIFIED_CLI_SERVER_AUTH_TOKEN")

    async def run():
        sent: list[dict] = []
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/v1/usage",
            "raw_path": b"/v1/usage",
            "query_string": b"",
            "headers": [(b"host", b"198.51.100.10")],
            "client": ("198.51.100.10", 12345),
            "server": ("0.0.0.0", 8000),
        }

        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        await server.app(scope, receive, send)
        return sent

    sent = asyncio.run(run())
    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 503


def test_raw_nonloopback_bind_is_rejected_even_with_loopback_proxy_headers(monkeypatch):
    """A raw uvicorn --host 0.0.0.0 launch cannot bypass via a local proxy."""
    monkeypatch.delenv("UNIFIED_CLI_ALLOW_EXTERNAL_BIND")
    monkeypatch.delenv("UNIFIED_CLI_SERVER_AUTH_TOKEN")

    async def run():
        sent: list[dict] = []
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/healthz",
            "raw_path": b"/healthz",
            "query_string": b"",
            "headers": [(b"host", b"127.0.0.1")],
            "client": ("127.0.0.1", 12345),
            "server": ("0.0.0.0", 8000),
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await server.app(scope, receive, send)
        return sent

    sent = asyncio.run(run())
    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 403


def test_external_run_and_lifespan_reject_missing_or_short_token(monkeypatch):
    monkeypatch.delenv("UNIFIED_CLI_SERVER_AUTH_TOKEN")
    with pytest.raises(UnifiedError, match="UNIFIED_CLI_SERVER_AUTH_TOKEN"):
        server.run(host="0.0.0.0")

    async def bad_lifespan():
        with pytest.raises(UnifiedError, match="UNIFIED_CLI_SERVER_AUTH_TOKEN"):
            async with server._lifespan(server.app):
                pass

    asyncio.run(bad_lifespan())

    monkeypatch.setenv("UNIFIED_CLI_SERVER_AUTH_TOKEN", "too-short")
    with pytest.raises(UnifiedError, match="at least 32 bytes"):
        server.run(host="0.0.0.0")


def test_explicit_token_protects_loopback_too(monkeypatch):
    monkeypatch.delenv("UNIFIED_CLI_ALLOW_EXTERNAL_BIND")

    async def run(headers):
        sent: list[dict] = []
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/healthz",
            "raw_path": b"/healthz",
            "query_string": b"",
            "headers": [(b"host", b"127.0.0.1"), *headers],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
        }

        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        await server.app(scope, receive, send)
        return sent

    unauthorized = asyncio.run(run([]))
    assert next(message for message in unauthorized if message["type"] == "http.response.start")["status"] == 401

    authorized = asyncio.run(run([
        (b"authorization", f"Bearer {_TEST_SERVER_AUTH_TOKEN}".encode("ascii")),
    ]))
    assert next(message for message in authorized if message["type"] == "http.response.start")["status"] == 200

    # No external opt-in and no configured token preserves the zero-config
    # loopback server behaviour.
    monkeypatch.delenv("UNIFIED_CLI_SERVER_AUTH_TOKEN")
    open_loopback = asyncio.run(run([]))
    assert next(message for message in open_loopback if message["type"] == "http.response.start")["status"] == 200


@pytest.mark.parametrize("url", [
    "/etc/hosts",
    "file:///etc/hosts",
    "https://example.test/image.png",
    "../private.png",
])
def test_untrusted_image_url_never_reaches_provider(client, monkeypatch, url):
    calls = {"send": 0}

    def fake_send(self, prompt, **kwargs):
        calls["send"] += 1
        return _response()

    monkeypatch.setattr(server.UnifiedConversation, "send", fake_send)
    response = client.post("/v1/chat/completions", json={
        "model": "haiku",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }],
    })
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_image"
    assert calls["send"] == 0


def test_malformed_image_url_object_is_a_400_not_a_500(client, monkeypatch):
    calls = {"send": 0}

    def fake_send(self, prompt, **kwargs):
        calls["send"] += 1
        return _response()

    monkeypatch.setattr(server.UnifiedConversation, "send", fake_send)
    response = client.post("/v1/chat/completions", json={
        "model": "haiku",
        "messages": [{
            "role": "user",
            "content": [{
                "type": "image_url",
                "image_url": "not-an-object",
            }],
        }],
    })
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_image"
    assert calls["send"] == 0


@pytest.mark.parametrize("model", ["gpt-5.4-mini", "gemini-3.5-flash"])
def test_agentic_providers_are_rejected_before_provider_spawn(client, monkeypatch, model):
    calls = {"send": 0}

    def fake_send(self, prompt, **kwargs):
        calls["send"] += 1
        return _response()

    monkeypatch.setattr(server.UnifiedConversation, "send", fake_send)
    response = client.post("/v1/chat/completions", json={
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "provider_disabled_for_server"
    assert calls["send"] == 0


def test_agentic_server_opt_in_reaches_the_explicit_provider_path(client, monkeypatch):
    calls = {"send": 0}
    monkeypatch.setenv("UNIFIED_CLI_SERVER_ALLOW_AGENTIC_PROVIDERS", "1")

    def fake_send(self, prompt, **kwargs):
        calls["send"] += 1
        return _response()

    monkeypatch.setattr(server.UnifiedConversation, "send", fake_send)
    response = client.post("/v1/chat/completions", json={
        "model": "gpt-5.4-mini",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert response.status_code == 200
    assert calls["send"] == 1


def test_models_endpoint_hides_disabled_agentic_providers(client, monkeypatch):
    requested: list[str] = []

    def fake_list_models(provider):
        requested.append(provider)
        return [ModelInfo(id=f"{provider}-model", provider=provider)]

    monkeypatch.setattr(server, "list_models", fake_list_models)
    response = client.get("/v1/models")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == ["claude-model"]
    assert requested == ["claude"]

    explicit_disabled = client.get("/v1/models?provider=codex")
    assert explicit_disabled.status_code == 200
    assert explicit_disabled.json()["data"] == []
    assert requested == ["claude"]

    invalid = client.get("/v1/models?provider=unknown")
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "invalid_provider"


def test_models_endpoint_includes_opted_in_agentic_providers(client, monkeypatch):
    requested: list[str] = []
    monkeypatch.setenv("UNIFIED_CLI_SERVER_ALLOW_AGENTIC_PROVIDERS", "1")

    def fake_list_models(provider):
        requested.append(provider)
        return [ModelInfo(id=f"{provider}-model", provider=provider)]

    monkeypatch.setattr(server, "list_models", fake_list_models)
    response = client.get("/v1/models")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == [
        "claude-model", "codex-model", "gemini-model",
    ]
    assert requested == ["claude", "codex", "gemini"]


async def _raw_asgi_chat(chunks: list[bytes], headers: list[tuple[bytes, bytes]]):
    events = iter([
        {"type": "http.request", "body": chunk,
         "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ])
    sent: list[dict] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "headers": [
            (b"host", b"127.0.0.1"),
            (b"authorization", f"Bearer {_TEST_SERVER_AUTH_TOKEN}".encode("ascii")),
            *headers,
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }

    async def receive():
        return next(events, {"type": "http.disconnect"})

    async def send(message):
        sent.append(message)

    await server.app(scope, receive, send)
    return sent


def test_chunked_body_overflow_rejected_before_routing(monkeypatch):
    monkeypatch.setattr(server, "_MAX_REQUEST_BODY_BYTES", 16)
    routed = {"count": 0}
    monkeypatch.setattr(
        server, "_route_builtin",
        lambda model: routed.__setitem__("count", routed["count"] + 1),
    )
    sent = asyncio.run(_raw_asgi_chat(
        [b"{" * 12, b"}" * 12],
        [(b"content-type", b"application/json")],
    ))
    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 413
    assert routed["count"] == 0


def test_content_length_overflow_does_not_read_request(monkeypatch):
    monkeypatch.setattr(server, "_MAX_REQUEST_BODY_BYTES", 16)
    received = {"count": 0}

    async def run():
        sent: list[dict] = []
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [
                (b"host", b"127.0.0.1"),
                (b"authorization", f"Bearer {_TEST_SERVER_AUTH_TOKEN}".encode("ascii")),
                (b"content-length", b"100"),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
        }

        async def receive():
            received["count"] += 1
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await server.app(scope, receive, send)
        return sent

    sent = asyncio.run(run())
    starts = [message for message in sent if message["type"] == "http.response.start"]
    bodies = [message for message in sent if message["type"] == "http.response.body"]
    payload_bodies = [message for message in bodies if message.get("body")]
    assert starts[0]["status"] == 413
    assert len(starts) == 1
    # Starlette may add one empty terminal ASGI frame; only one payload frame
    # proves the middleware did not attempt a second 413 response.
    assert len(payload_bodies) == 1
    assert received["count"] == 0


def test_same_conversation_is_rejected_while_first_turn_active(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def blocking_send(self, prompt, **kwargs):
        entered.set()
        assert release.wait(5)
        return _response()

    monkeypatch.setattr(server.UnifiedConversation, "send", blocking_send)
    request = server.ChatRequest(
        model="haiku", messages=[{"role": "user", "content": "hi"}],
        user="shared",
    )

    def first_request():
        try:
            server.chat_completions(request)
        except BaseException as exc:  # pragma: no cover - assertion aid
            errors.append(exc)

    worker = threading.Thread(target=first_request)
    worker.start()
    assert entered.wait(3)
    with pytest.raises(HTTPException) as exc_info:
        server.chat_completions(request)
    assert exc_info.value.status_code == 409
    release.set()
    worker.join(timeout=5)
    assert not errors
    assert server._ACTIVE_TURNS == 0
    assert server._ACTIVE_PROVIDER_TURNS == {}
    assert not server.CONVS["shared"].active


def test_same_provider_is_rejected_across_different_users(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def blocking_send(self, prompt, **kwargs):
        entered.set()
        assert release.wait(5)
        return _response()

    monkeypatch.setattr(server.UnifiedConversation, "send", blocking_send)
    first = server.ChatRequest(
        model="haiku", messages=[{"role": "user", "content": "one"}],
        user="first-user",
    )
    second = server.ChatRequest(
        model="sonnet", messages=[{"role": "user", "content": "two"}],
        user="second-user",
    )
    worker = threading.Thread(target=server.chat_completions, args=(first,))
    worker.start()
    assert entered.wait(3)
    try:
        with pytest.raises(HTTPException) as exc_info:
            server.chat_completions(second)
        assert exc_info.value.status_code == 429
        assert exc_info.value.detail["code"] == "provider_busy"
        assert exc_info.value.headers == {"Retry-After": "1"}
        assert server._ACTIVE_TURNS == 1
        assert server._ACTIVE_PROVIDER_TURNS == {"claude": 1}
        assert "second-user" not in server.CONVS
    finally:
        release.set()
        worker.join(timeout=5)


def test_different_providers_can_run_within_global_ceiling(monkeypatch):
    monkeypatch.setenv("UNIFIED_CLI_SERVER_ALLOW_AGENTIC_PROVIDERS", "1")
    entered = {"claude": threading.Event(), "codex": threading.Event()}
    release = threading.Event()
    errors: list[BaseException] = []

    def blocking_send(self, prompt, **kwargs):
        provider = kwargs["provider"]
        entered[provider].set()
        assert release.wait(5)
        return _response()

    def request(model, user):
        try:
            server.chat_completions(server.ChatRequest(
                model=model,
                messages=[{"role": "user", "content": "hi"}],
                user=user,
            ))
        except BaseException as exc:  # pragma: no cover - assertion aid
            errors.append(exc)

    monkeypatch.setattr(server.UnifiedConversation, "send", blocking_send)
    workers = [
        threading.Thread(target=request, args=("haiku", "claude-user")),
        threading.Thread(target=request, args=("gpt-5.4-mini", "codex-user")),
    ]
    for worker in workers:
        worker.start()
    assert entered["claude"].wait(3)
    assert entered["codex"].wait(3)
    assert server._ACTIVE_TURNS == 2
    assert server._ACTIVE_PROVIDER_TURNS == {"claude": 1, "codex": 1}
    release.set()
    for worker in workers:
        worker.join(timeout=5)
    assert not errors
    assert server._ACTIVE_TURNS == 0
    assert server._ACTIVE_PROVIDER_TURNS == {}


def test_provider_alias_and_failed_acquisitions_do_not_consume_slots(monkeypatch):
    gemini = server._acquire_conversation("gemini-user", "gemini")
    try:
        with pytest.raises(HTTPException) as alias_busy:
            server._acquire_conversation("agy-user", "agy")
        assert alias_busy.value.status_code == 429
        assert alias_busy.value.detail["code"] == "provider_busy"
        assert server._ACTIVE_TURNS == 1
        assert server._ACTIVE_PROVIDER_TURNS == {"gemini": 1}
        assert "agy-user" not in server.CONVS
    finally:
        gemini.release()

    monkeypatch.setattr(server, "_MAX_ACTIVE_TURNS", 1)
    claude = server._acquire_conversation("claude-user", "claude")
    try:
        with pytest.raises(HTTPException) as global_busy:
            server._acquire_conversation("codex-user", "codex")
        assert global_busy.value.status_code == 429
        assert global_busy.value.detail["code"] == "server_busy"
        assert server._ACTIVE_TURNS == 1
        assert server._ACTIVE_PROVIDER_TURNS == {"claude": 1}
        assert "codex-user" not in server.CONVS
    finally:
        claude.release()


def test_same_provider_acquisition_race_has_one_winner():
    worker_count = 24
    start = threading.Barrier(worker_count)
    finished_attempt = threading.Condition()
    release = threading.Event()
    leases: list[server._ConversationLease] = []
    errors: list[HTTPException] = []
    attempt_count = 0

    def compete(index):
        nonlocal attempt_count
        start.wait()
        try:
            lease = server._acquire_conversation(f"race-{index}", "claude")
        except HTTPException as exc:
            with finished_attempt:
                errors.append(exc)
                attempt_count += 1
                finished_attempt.notify_all()
            return
        with finished_attempt:
            leases.append(lease)
            attempt_count += 1
            finished_attempt.notify_all()
        assert release.wait(5)
        lease.release()

    workers = [threading.Thread(target=compete, args=(index,))
               for index in range(worker_count)]
    for worker in workers:
        worker.start()
    with finished_attempt:
        assert finished_attempt.wait_for(
            lambda: attempt_count == worker_count, timeout=5)
    assert len(leases) == 1
    assert len(errors) == worker_count - 1
    assert all(error.status_code == 429 for error in errors)
    assert all(error.detail["code"] == "provider_busy" for error in errors)
    assert server._ACTIVE_TURNS == 1
    assert server._ACTIVE_PROVIDER_TURNS == {"claude": 1}
    release.set()
    for worker in workers:
        worker.join(timeout=5)
    leases[0].release()  # idempotent after the winning worker released it
    assert server._ACTIVE_TURNS == 0
    assert server._ACTIVE_PROVIDER_TURNS == {}


def test_active_conversation_is_never_lru_evicted(monkeypatch):
    monkeypatch.setattr(server, "_MAX_CONVS", 1)
    lease = server._acquire_conversation("active")
    try:
        with pytest.raises(HTTPException) as exc_info:
            server._acquire_conversation("new", "codex")
        assert exc_info.value.status_code == 503
        assert "active" in server.CONVS
        assert "new" not in server.CONVS
    finally:
        lease.release()


def test_stream_response_limit_closes_upstream_and_marks_length(client, monkeypatch):
    monkeypatch.setattr(server, "_MAX_RESPONSE_CHARS", 3)
    closed = threading.Event()

    def stream_with_cleanup(self, prompt, **kwargs):
        try:
            yield Message(kind="text", provider="claude", text="abcdef")
        finally:
            closed.set()

    monkeypatch.setattr(server.UnifiedConversation, "stream", stream_with_cleanup)
    response = client.post("/v1/chat/completions", json={
        "model": "haiku",
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert response.status_code == 200
    assert '"content": "abc"' in response.text
    assert '"finish_reason": "length"' in response.text
    assert "data: [DONE]" in response.text
    assert closed.is_set()
    assert server._ACTIVE_TURNS == 0
    assert server._ACTIVE_PROVIDER_TURNS == {}


def test_nonstream_response_limit_releases_provider(client, monkeypatch):
    monkeypatch.setattr(server, "_MAX_RESPONSE_CHARS", 3)
    monkeypatch.setattr(
        server.UnifiedConversation, "send",
        lambda *_args, **_kwargs: _response("abcdef"),
    )
    response = client.post("/v1/chat/completions", json={
        "model": "haiku",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "abc"
    assert response.json()["choices"][0]["finish_reason"] == "length"
    assert server._ACTIVE_TURNS == 0
    assert server._ACTIVE_PROVIDER_TURNS == {}


def test_stream_provider_error_releases_provider(client, monkeypatch):
    def failed_stream(self, prompt, **kwargs):
        yield Message(
            kind="error", provider="claude", error="provider failed",
        )

    monkeypatch.setattr(server.UnifiedConversation, "stream", failed_stream)
    response = client.post("/v1/chat/completions", json={
        "model": "haiku",
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert response.status_code == 200
    assert '"code": "upstream_error"' in response.text
    assert server._ACTIVE_TURNS == 0
    assert server._ACTIVE_PROVIDER_TURNS == {}


def test_uniterated_stream_response_releases_provider_on_abandonment(monkeypatch):
    monkeypatch.setattr(
        server.UnifiedConversation,
        "stream",
        lambda *_args, **_kwargs: pytest.fail("stream must not be iterated"),
    )
    response = server.chat_completions(server.ChatRequest(
        model="haiku",
        stream=True,
        messages=[{"role": "user", "content": "hi"}],
        user="abandoned",
    ))
    assert server._ACTIVE_PROVIDER_TURNS == {"claude": 1}
    del response
    gc.collect()
    assert server._ACTIVE_TURNS == 0
    assert server._ACTIVE_PROVIDER_TURNS == {}
    assert not server.CONVS["abandoned"].active


def test_stream_task_cancellation_cleans_up_before_provider_release(monkeypatch):
    cleaned = threading.Event()

    def endless_stream(self, prompt, **kwargs):
        try:
            while True:
                yield Message(kind="text", provider="claude", text="chunk")
        finally:
            assert server._ACTIVE_PROVIDER_TURNS == {"claude": 1}
            cleaned.set()

    monkeypatch.setattr(server.UnifiedConversation, "stream", endless_stream)
    response = server.chat_completions(server.ChatRequest(
        model="haiku",
        stream=True,
        messages=[{"role": "user", "content": "hi"}],
        user="cancelled-stream",
    ))

    async def cancel_response():
        first_body = asyncio.Event()
        never_disconnect = asyncio.Event()

        async def receive():
            await never_disconnect.wait()

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                first_body.set()
                await never_disconnect.wait()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [],
        }
        task = asyncio.create_task(response(scope, receive, send))
        await asyncio.wait_for(first_body.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_response())
    assert cleaned.is_set()
    assert server._ACTIVE_TURNS == 0
    assert server._ACTIVE_PROVIDER_TURNS == {}
    assert not server.CONVS["cancelled-stream"].active
    reacquired = server._acquire_conversation("after-cancel", "claude")
    reacquired.release()


def test_retained_stream_disconnect_cleans_up_before_provider_release(monkeypatch):
    cleaned = threading.Event()

    def endless_stream(self, prompt, **kwargs):
        try:
            while True:
                yield Message(kind="text", provider="claude", text="chunk")
        finally:
            assert server._ACTIVE_PROVIDER_TURNS == {"claude": 1}
            cleaned.set()

    monkeypatch.setattr(server.UnifiedConversation, "stream", endless_stream)
    response = server.chat_completions(server.ChatRequest(
        model="haiku",
        stream=True,
        messages=[{"role": "user", "content": "hi"}],
        user="disconnected-stream",
    ))

    async def disconnect_response():
        first_body = asyncio.Event()

        async def receive():
            await first_body.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                first_body.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [],
        }
        await asyncio.wait_for(response(scope, receive, send), timeout=3)

    asyncio.run(disconnect_response())
    # Keep `response` alive: cleanup must not depend on response GC.
    assert response is not None
    assert cleaned.is_set()
    assert server._ACTIVE_TURNS == 0
    assert server._ACTIVE_PROVIDER_TURNS == {}
    assert not server.CONVS["disconnected-stream"].active
    reacquired = server._acquire_conversation("after-disconnect", "claude")
    reacquired.release()


def test_stream_send_disconnect_cleans_up_before_provider_release(monkeypatch):
    cleaned = threading.Event()

    def endless_stream(self, prompt, **kwargs):
        try:
            while True:
                yield Message(kind="text", provider="claude", text="chunk")
        finally:
            assert server._ACTIVE_PROVIDER_TURNS == {"claude": 1}
            cleaned.set()

    monkeypatch.setattr(server.UnifiedConversation, "stream", endless_stream)
    response = server.chat_completions(server.ChatRequest(
        model="haiku",
        stream=True,
        messages=[{"role": "user", "content": "hi"}],
        user="send-disconnected-stream",
    ))

    async def disconnect_response():
        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                raise OSError("client disconnected")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [],
        }
        with pytest.raises(ClientDisconnect):
            await response(scope, receive, send)

    asyncio.run(disconnect_response())
    assert response is not None
    assert cleaned.is_set()
    assert server._ACTIVE_TURNS == 0
    assert server._ACTIVE_PROVIDER_TURNS == {}
    assert not server.CONVS["send-disconnected-stream"].active
    reacquired = server._acquire_conversation("after-send-disconnect", "claude")
    reacquired.release()


def test_stream_response_construction_failure_closes_before_release(monkeypatch):
    cleaned = threading.Event()

    class Content:
        def close(self):
            assert server._ACTIVE_PROVIDER_TURNS == {"claude": 1}
            cleaned.set()

    lease = server._acquire_conversation("construction-failure", "claude")

    def fail_construction(*args, **kwargs):
        raise RuntimeError("construction failed")

    monkeypatch.setattr(server.StreamingResponse, "__init__", fail_construction)
    with pytest.raises(RuntimeError, match="construction failed"):
        server._LeasedStreamingResponse(Content(), lease)
    assert cleaned.is_set()
    assert server._ACTIVE_TURNS == 0
    assert server._ACTIVE_PROVIDER_TURNS == {}


def test_async_shape_discovery_failure_closes_and_releases():
    cleaned = threading.Event()

    class Content:
        @property
        def aclose(self):
            raise RuntimeError("aclose lookup failed")

        def close(self):
            assert server._ACTIVE_PROVIDER_TURNS == {"claude": 1}
            cleaned.set()

    lease = server._acquire_conversation("raising-aclose", "claude")
    with pytest.raises(TypeError, match="must be synchronous"):
        server._LeasedStreamingResponse(Content(), lease)
    assert cleaned.is_set()
    assert server._ACTIVE_TURNS == 0
    assert server._ACTIVE_PROVIDER_TURNS == {}
    assert server._ACTIVE_LEASES == {}
    assert not server.CONVS["raising-aclose"].active


def test_async_stream_content_is_rejected_before_asgi_and_releases():
    cleaned = threading.Event()

    class Content:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        def close(self):
            assert server._ACTIVE_PROVIDER_TURNS == {"claude": 1}
            cleaned.set()

    lease = server._acquire_conversation("async-content", "claude")
    with pytest.raises(TypeError, match="must be synchronous"):
        server._LeasedStreamingResponse(Content(), lease)
    assert cleaned.is_set()
    assert server._ACTIVE_TURNS == 0
    assert server._ACTIVE_PROVIDER_TURNS == {}
    assert server._ACTIVE_LEASES == {}
    assert not server.CONVS["async-content"].active

    class AcloseOnly:
        async def aclose(self):
            pytest.fail("aclose-only content must never be scheduled")

    lease = server._acquire_conversation("aclose-only", "claude")
    with pytest.raises(TypeError, match="must be synchronous"):
        server._LeasedStreamingResponse(AcloseOnly(), lease)
    assert server._ACTIVE_TURNS == 0
    assert server._ACTIVE_PROVIDER_TURNS == {}
    assert server._ACTIVE_LEASES == {}
    assert not server.CONVS["aclose-only"].active


def test_finalize_registration_failure_preserves_error_and_cleans_lease(monkeypatch):
    cleaned = threading.Event()

    class Content:
        def __iter__(self):
            return iter(())

        def close(self):
            assert server._ACTIVE_PROVIDER_TURNS == {"claude": 1}
            cleaned.set()
            raise RuntimeError("cleanup failure must not mask construction")

    def fail_finalize(*args, **kwargs):
        raise RuntimeError("finalize registration failed")

    monkeypatch.setattr(server.weakref, "finalize", fail_finalize)
    lease = server._acquire_conversation("finalize-failure", "claude")
    with pytest.raises(RuntimeError, match="finalize registration failed"):
        server._LeasedStreamingResponse(Content(), lease)
    assert cleaned.is_set()
    assert server._ACTIVE_TURNS == 0
    assert server._ACTIVE_PROVIDER_TURNS == {}
    assert server._ACTIVE_LEASES == {}
    assert not server.CONVS["finalize-failure"].active


def test_stream_cleanup_construction_failure_preserves_error_and_lease(monkeypatch):
    cleaned = threading.Event()

    class Content:
        def close(self):
            assert server._ACTIVE_PROVIDER_TURNS == {"claude": 1}
            cleaned.set()

    def fail_cleanup_construction(self, content, lease):
        raise RuntimeError("cleanup construction failed")

    monkeypatch.setattr(
        server._StreamLeaseCleanup, "__init__", fail_cleanup_construction,
    )
    lease = server._acquire_conversation("cleanup-construction", "claude")
    with pytest.raises(RuntimeError, match="cleanup construction failed"):
        server._LeasedStreamingResponse(Content(), lease)
    assert cleaned.is_set()
    assert server._ACTIVE_TURNS == 0
    assert server._ACTIVE_PROVIDER_TURNS == {}
    assert server._ACTIVE_LEASES == {}
    assert not server.CONVS["cleanup-construction"].active


def test_shutdown_releases_all_provider_reservations():
    lease = server._acquire_conversation("shutdown", "claude")

    async def shutdown():
        async with server._lifespan(server.app):
            assert server._ACTIVE_PROVIDER_TURNS == {"claude": 1}

    asyncio.run(shutdown())
    assert lease._released
    assert server._ACTIVE_TURNS == 0
    assert server._ACTIVE_PROVIDER_TURNS == {}
    assert not server.CONVS["shutdown"].active


def test_lease_releases_after_provider_error(monkeypatch):
    def boom(self, prompt, **kwargs):
        raise UnifiedError(kind="network", provider="claude", message="boom")

    monkeypatch.setattr(server.UnifiedConversation, "send", boom)
    request = server.ChatRequest(
        model="haiku", messages=[{"role": "user", "content": "hi"}],
        user="error-case",
    )
    with pytest.raises(HTTPException) as exc_info:
        server.chat_completions(request)
    assert exc_info.value.status_code == 502
    assert server._ACTIVE_TURNS == 0
    assert server._ACTIVE_PROVIDER_TURNS == {}
    assert not server.CONVS["error-case"].active


def test_server_history_limits_bound_retained_state():
    conv = UnifiedConversation(max_turns=2, max_turn_chars=3)
    conv._record("claude", "first", "reply-one", None)
    conv._record("claude", "second", "reply-two", None)
    conv._record("claude", "third", "reply-three", None)
    assert [turn.prompt for turn in conv.turns] == ["se…", "th…"]
    assert [turn.text for turn in conv.turns] == ["re…", "re…"]
