import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from unified_cli_ext import (
    CancellationToken,
    ConfigurationError,
    HttpSseClient,
    LimitExceeded,
    ProtocolError,
    SseEvent,
    TransportCancelled,
    TransportLimits,
)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def send_bytes(self, status, content_type, body, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/json":
            self.send_bytes(200, "application/json", b'{"ok":true}')
        elif self.path == "/redirect":
            self.send_bytes(302, "text/plain", b"", {"Location": "/json"})
        elif self.path == "/cross-redirect":
            self.send_bytes(302, "text/plain", b"", {"Location": "http://127.0.0.1:1/json"})
        elif self.path == "/bad":
            self.send_bytes(200, "application/json", b"not-json")
        elif self.path == "/big":
            self.send_bytes(200, "application/json", b'"' + b"x" * 1000 + b'"')
        elif self.path == "/nan":
            self.send_bytes(200, "application/json", b"NaN")
        elif self.path == "/duplicate-key":
            self.send_bytes(200, "application/json", b'{"ok":true,"ok":false}')
        elif self.path == "/slow-json":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                self.wfile.write(b'{"ok":')
                self.wfile.flush()
                time.sleep(5)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif self.path == "/sse":
            body = b"id: 7\nevent: token\ndata: one\ndata: two\nretry: 25\n\ndata: done\n\n"
            self.send_bytes(200, "text/event-stream", body)
        elif self.path == "/slow-sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                self.wfile.write(b"data: first\n\n")
                self.wfile.flush()
                time.sleep(5)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif self.path == "/drip-sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                for _ in range(20):
                    self.wfile.write(b": keepalive\n")
                    self.wfile.flush()
                    time.sleep(0.03)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif self.path == "/exact-line":
            self.send_bytes(200, "text/event-stream", b"data: " + b"x" * 10)
        elif self.path == "/huge-retry":
            self.send_bytes(
                200,
                "text/event-stream",
                b"retry: " + b"9" * 10000 + b"\ndata: safe\n\n",
            )
        elif self.path == "/sse-cr-bom":
            self.send_bytes(
                200,
                "text/event-stream",
                b"\xef\xbb\xbfid: 9\revent: token\rdata: one\rdata: two\r"
                b"retry: 20\r\rdata: done\r\r",
            )
        else:
            self.send_bytes(404, "text/plain", b"missing")


@pytest.fixture
def loopback_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:{}".format(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_http_json_redirect_and_malformed_bounds(loopback_server):
    client = HttpSseClient(loopback_server)
    assert client.request_json("/json") == {"ok": True}
    assert client.request_json("/redirect") == {"ok": True}
    with pytest.raises(ProtocolError, match="cross-origin"):
        client.request_json("/cross-redirect", headers={"Authorization": "secret"})
    with pytest.raises(ProtocolError):
        client.request_json("/bad")
    with pytest.raises(ProtocolError):
        client.request_json("/nan")
    with pytest.raises(ProtocolError):
        client.request_json("/duplicate-key")
    with pytest.raises(LimitExceeded):
        HttpSseClient(
            loopback_server,
            limits=TransportLimits(max_body_bytes=100),
        ).request_json("/big")
    with pytest.raises(ProtocolError):
        client.request_json("/json", method="POST", value={"number": float("nan")})


@pytest.mark.parametrize(
    "url",
    ["http://example.com", "http://8.8.8.8", "file:///tmp/x", "http://user:pass@127.0.0.1"],
)
def test_http_contract_refuses_non_loopback_or_credential_urls(url):
    with pytest.raises(ConfigurationError):
        HttpSseClient(url)


def test_sse_sync_async_parity(loopback_server):
    expected = [
        SseEvent("token", "one\ntwo", "7", 25),
        SseEvent("message", "done", "7", None),
    ]
    assert list(HttpSseClient(loopback_server).iter_sse("/sse")) == expected

    async def collect():
        return [item async for item in HttpSseClient(loopback_server).aiter_sse("/sse")]

    assert asyncio.run(collect()) == expected


def test_sse_early_close_and_explicit_cancel(loopback_server):
    client = HttpSseClient(loopback_server)
    stream = client.iter_sse("/slow-sse")
    assert next(stream).data == "first"
    started = time.monotonic()
    stream.close()
    assert time.monotonic() - started < 1

    token = CancellationToken()
    client = HttpSseClient(loopback_server, cancellation=token)
    stream = client.iter_sse("/slow-sse")
    assert next(stream).data == "first"
    token.cancel()
    with pytest.raises(TransportCancelled):
        next(stream)


def test_sse_whole_operation_deadline_and_exact_unterminated_line(loopback_server):
    started = time.monotonic()
    with pytest.raises(Exception, match="timed out"):
        list(HttpSseClient(loopback_server, timeout=0.12).iter_sse("/drip-sse"))
    assert time.monotonic() - started < 0.5
    limits = TransportLimits(max_line_bytes=16)
    with pytest.raises(LimitExceeded):
        list(HttpSseClient(loopback_server, limits=limits).iter_sse("/exact-line"))


def test_sse_ignores_oversized_retry_integer_without_parsing_it(loopback_server):
    assert list(HttpSseClient(loopback_server).iter_sse("/huge-retry")) == [
        SseEvent("message", "safe", None, None)
    ]


def test_sse_accepts_initial_bom_and_cr_only_line_endings(loopback_server):
    assert list(HttpSseClient(loopback_server).iter_sse("/sse-cr-bom")) == [
        SseEvent("token", "one\ntwo", "9", 20),
        SseEvent("message", "done", "9", None),
    ]


def test_header_names_are_case_insensitive_and_controls_rejected(loopback_server):
    with pytest.raises(ConfigurationError):
        HttpSseClient(loopback_server).request_json(
            "/json", headers={"accept": "application/json", "Accept": "application/json"}
        )
    with pytest.raises(ConfigurationError):
        HttpSseClient(loopback_server).request_json("/json", headers={"X-Test": "bad\x85"})
    with pytest.raises(ConfigurationError):
        HttpSseClient(loopback_server).request_json("/json", headers={"X-Test": "한글"})


def test_http_async_cancellation_closes_inflight_request(loopback_server):
    finished = threading.Event()

    async def run():
        client = HttpSseClient(loopback_server, timeout=5)
        original = client.request_json

        def tracked_request(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            finally:
                finished.set()

        client.request_json = tracked_request
        task = asyncio.create_task(client.request_json_async("/slow-json"))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - started < 0.5
        assert client.cancellation.cancelled

    asyncio.run(run())
    assert finished.wait(1), "cancelled HTTP worker remained active"
