"""Dependency-free loopback HTTP/JSON and Server-Sent Events client."""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import json
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Iterator, Mapping, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from ..errors import ConfigurationError, LimitExceeded, ProtocolError, TransportError, TransportTimeout
from .security import (
    CancellationToken,
    TransportLimits,
    strict_json_loads,
    validate_positive_timeout,
)
from ..normalization.validation import validate_unicode
from ..normalization.events import freeze_json


_HEADER_NAME = __import__("re").compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_UTF8_BOM = b"\xef\xbb\xbf"


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


@dataclass(frozen=True)
class SseEvent:
    event: str
    data: str
    event_id: Optional[str] = None
    retry_ms: Optional[int] = None

    def __post_init__(self) -> None:
        validate_unicode(self.event, label="SSE event name", maximum=256, empty=False)
        validate_unicode(
            self.data,
            label="SSE event data",
            maximum=16 * 1024 * 1024,
            empty=True,
            allow_text_newlines=True,
        )
        if self.event_id is not None:
            validate_unicode(
                self.event_id, label="SSE event id", maximum=1024, empty=True
            )
        if self.retry_ms is not None and (
            type(self.retry_ms) is not int or not 0 <= self.retry_ms <= 3_600_000
        ):
            raise ProtocolError("SSE retry must be a bounded nonnegative integer")


def _validate_loopback_url(url: str) -> Tuple[str, str, int, str]:
    if type(url) is not str:
        raise ConfigurationError("invalid HTTP loopback URL")
    try:
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ConfigurationError("invalid HTTP loopback URL") from None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigurationError("HTTP transport requires an http(s) loopback URL")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ConfigurationError("HTTP URL may not contain credentials or a fragment")
    host = parsed.hostname
    # Requiring a literal pins the connected address and removes DNS-rebinding
    # and validation/use races (including a mutable localhost hosts entry).
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ConfigurationError("HTTP host must be a loopback IP literal") from None
    if not address.is_loopback:
        raise ConfigurationError("HTTP transport refuses non-loopback addresses")
    if not 1 <= port <= 65535:
        raise ConfigurationError("HTTP port is out of range")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    try:
        validate_unicode(path, label="HTTP request target", maximum=16 * 1024, empty=False)
        path.encode("ascii", "strict")
    except (ProtocolError, UnicodeError):
        raise ConfigurationError("invalid HTTP request target") from None
    return parsed.scheme, host, port, path


def _headers(value: Optional[Mapping[str, str]]) -> Dict[str, str]:
    result = {}
    seen = set()
    try:
        source = value if value is not None else {}
        iterator = iter(source.items())
        for index, pair in enumerate(iterator):
            if index >= 256:
                raise ConfigurationError("HTTP headers exceed 256 entries")
            try:
                key, item = pair
            except (TypeError, ValueError):
                raise ConfigurationError("HTTP headers are malformed") from None
            lower = key.lower() if type(key) is str else ""
            if (
                type(key) is not str
                or type(item) is not str
                or not _HEADER_NAME.fullmatch(key)
                or "\r" in key + item
                or "\n" in key + item
                or lower in {"host", "content-length", "connection"}
                or lower in seen
            ):
                raise ConfigurationError("invalid HTTP header")
            try:
                validate_unicode(item, label="HTTP header value", maximum=8192, empty=True)
                item.encode("latin-1", "strict")
            except (ProtocolError, UnicodeError):
                raise ConfigurationError("invalid HTTP header") from None
            seen.add(lower)
            result[key] = item
    except ConfigurationError:
        raise
    except Exception:
        raise ConfigurationError("HTTP headers are malformed") from None
    return result


def _setdefault_header(headers: Dict[str, str], name: str, value: str) -> None:
    if name.lower() not in {key.lower() for key in headers}:
        headers[name] = value


class HttpSseClient:
    """Loopback-only client with bounded redirects, bodies, and SSE events."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        limits: TransportLimits = TransportLimits(),
        cancellation: Optional[CancellationToken] = None,
    ) -> None:
        _validate_loopback_url(base_url)
        if type(limits) is not TransportLimits:
            raise ConfigurationError("limits must be TransportLimits")
        self.base_url = base_url
        self._origin = _validate_loopback_url(base_url)[:3]
        self.timeout = validate_positive_timeout(timeout)
        self.limits = limits
        if cancellation is not None and type(cancellation) is not CancellationToken:
            raise ConfigurationError("cancellation must be CancellationToken")
        self.cancellation = cancellation if cancellation is not None else CancellationToken()

    def _connection(self, url: str) -> Tuple[http.client.HTTPConnection, str]:
        scheme, host, port, path = _validate_loopback_url(url)
        cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        return cls(host, port, timeout=self.timeout), path

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TransportTimeout("loopback HTTP operation timed out")
        return remaining

    def _watch_cancel(
        self, connection: http.client.HTTPConnection
    ) -> Tuple[threading.Event, threading.Thread]:
        stop = threading.Event()

        def watch() -> None:
            while not stop.wait(0.02):
                if self.cancellation.cancelled:
                    try:
                        connection.close()
                    except OSError:
                        pass
                    return

        thread = threading.Thread(target=watch, daemon=True)
        thread.start()
        return stop, thread

    def _iter_sse_lines(
        self,
        response: http.client.HTTPResponse,
        connection: http.client.HTTPConnection,
        deadline: float,
    ) -> Iterator[Tuple[bytes, int]]:
        """Yield bounded lines split on CRLF, LF, or CR per the SSE grammar."""

        line = bytearray()
        pending_cr = False
        first_line = True
        total = 0

        def finish(raw_size: int) -> Tuple[bytes, int]:
            nonlocal first_line
            payload = bytes(line)
            line.clear()
            if first_line:
                first_line = False
                if payload.startswith(_UTF8_BOM):
                    payload = payload[len(_UTF8_BOM) :]
            return payload, raw_size

        while True:
            self.cancellation.raise_if_cancelled()
            remaining = self._remaining(deadline)
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(
                min(65536, self.limits.max_output_bytes - total + 1)
            )
            if not chunk:
                if pending_cr:
                    raw_size = len(line) + 1
                    if raw_size > self.limits.max_line_bytes:
                        raise LimitExceeded("SSE line exceeds configured limit")
                    yield finish(raw_size)
                elif line:
                    if len(line) >= self.limits.max_line_bytes:
                        raise LimitExceeded("SSE line exceeds configured limit")
                    yield finish(len(line))
                return
            total += len(chunk)
            if total > self.limits.max_output_bytes:
                raise LimitExceeded("SSE stream exceeds configured limit")

            for byte in chunk:
                if pending_cr:
                    raw_size = len(line) + (2 if byte == 10 else 1)
                    if raw_size > self.limits.max_line_bytes:
                        raise LimitExceeded("SSE line exceeds configured limit")
                    yield finish(raw_size)
                    pending_cr = False
                    if byte == 10:
                        continue
                if byte == 13:
                    pending_cr = True
                elif byte == 10:
                    raw_size = len(line) + 1
                    if raw_size > self.limits.max_line_bytes:
                        raise LimitExceeded("SSE line exceeds configured limit")
                    yield finish(raw_size)
                else:
                    line.append(byte)
                    # With no terminator yet, a line exactly at the ceiling is
                    # already too large to terminate within the configured cap.
                    if len(line) >= self.limits.max_line_bytes:
                        raise LimitExceeded("SSE line exceeds configured limit")

    def _open(
        self,
        method: str,
        path: str,
        body: Optional[bytes],
        headers: Mapping[str, str],
        deadline: float,
    ) -> Tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        if type(method) is not str or method not in ("GET", "POST"):
            raise ConfigurationError("HTTP method must be GET or POST")
        if type(path) is not str:
            raise ConfigurationError("HTTP request path must be a string")
        url = urljoin(self.base_url.rstrip("/") + "/", path)
        original_origin = _validate_loopback_url(url)[:3]
        if original_origin != self._origin:
            raise ConfigurationError("HTTP request target must remain on the configured origin")
        redirects = 0
        while True:
            self.cancellation.raise_if_cancelled()
            connection, target = self._connection(url)
            connection.timeout = self._remaining(deadline)
            stop, watcher = self._watch_cancel(connection)
            try:
                connection.request(method, target, body=body, headers=dict(headers))
                response = connection.getresponse()
            except (OSError, UnicodeError, http.client.HTTPException) as exc:
                connection.close()
                if self.cancellation.cancelled:
                    self.cancellation.raise_if_cancelled()
                if isinstance(exc, (TimeoutError, socket.timeout)):
                    raise TransportTimeout("loopback HTTP request timed out") from exc
                raise TransportError("loopback HTTP request failed") from exc
            finally:
                stop.set()
                watcher.join(timeout=0.1)
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                status = response.status
                response.close()
                connection.close()
                if not location or redirects >= self.limits.max_redirects:
                    raise ProtocolError("HTTP redirect limit exceeded or location missing")
                url = urljoin(url, location)
                if _validate_loopback_url(url)[:3] != original_origin:
                    raise ProtocolError("cross-origin HTTP redirect refused")
                redirects += 1
                if status == 303:
                    method, body = "GET", None
                continue
            if response.status < 200 or response.status >= 300:
                status = response.status
                response.close()
                connection.close()
                raise TransportError("loopback HTTP peer returned status {}".format(status))
            return connection, response

    def request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        value: Any = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        request_headers = _headers(headers)
        _setdefault_header(request_headers, "Accept", "application/json")
        body = None
        if value is not None:
            try:
                bounded = _plain_json(freeze_json(value, drop_reasoning=False))
                body = json.dumps(bounded, separators=(",", ":"), allow_nan=False).encode("utf-8")
            except (TypeError, ValueError, UnicodeError, ProtocolError):
                raise ProtocolError("HTTP request value is not bounded JSON") from None
            if len(body) > self.limits.max_body_bytes:
                raise LimitExceeded("HTTP request body exceeds configured limit")
            _setdefault_header(request_headers, "Content-Type", "application/json")
        deadline = time.monotonic() + self.timeout
        connection, response = self._open(method, path, body, request_headers, deadline)
        content_types = response.headers.get_all("Content-Type") or []
        media_type = content_types[0].split(";", 1)[0].strip().lower() if len(content_types) == 1 else ""
        if not (media_type == "application/json" or media_type.endswith("+json")):
            response.close()
            connection.close()
            raise ProtocolError("HTTP response content type is not JSON")
        stop, watcher = self._watch_cancel(connection)
        chunks = []
        size = 0
        try:
            while True:
                self.cancellation.raise_if_cancelled()
                remaining = self._remaining(deadline)
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read(min(65536, self.limits.max_body_bytes - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > self.limits.max_body_bytes:
                    raise LimitExceeded("HTTP response body exceeds configured limit")
                chunks.append(chunk)
        except (OSError, http.client.HTTPException) as exc:
            if self.cancellation.cancelled:
                self.cancellation.raise_if_cancelled()
            if isinstance(exc, (TimeoutError, socket.timeout)):
                raise TransportTimeout("loopback HTTP operation timed out") from None
            raise TransportError("loopback HTTP response failed") from exc
        finally:
            stop.set()
            watcher.join(timeout=0.1)
            response.close()
            connection.close()
        try:
            result = strict_json_loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise ProtocolError("HTTP response is not valid bounded JSON") from None
        try:
            return _plain_json(freeze_json(result, drop_reasoning=False))
        except ProtocolError:
            raise ProtocolError("HTTP response is not valid bounded JSON") from None

    async def request_json_async(self, path: str, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        worker = loop.run_in_executor(None, lambda: self.request_json(path, **kwargs))
        try:
            return await worker
        except asyncio.CancelledError:
            # Closing is performed by the request's cancellation watcher.  The
            # caller regains control promptly while the worker unwinds without
            # leaving a live socket behind.
            self.cancellation.cancel()
            if not worker.done():
                worker.add_done_callback(
                    lambda future: future.exception() if not future.cancelled() else None
                )
            raise

    def iter_sse(
        self,
        path: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Iterator[SseEvent]:
        request_headers = _headers(headers)
        _setdefault_header(request_headers, "Accept", "text/event-stream")
        deadline = time.monotonic() + self.timeout
        connection, response = self._open("GET", path, None, request_headers, deadline)
        content_types = response.headers.get_all("Content-Type") or []
        media_type = content_types[0].split(";", 1)[0].strip().lower() if len(content_types) == 1 else ""
        if media_type != "text/event-stream":
            response.close()
            connection.close()
            raise ProtocolError("HTTP response content type is not text/event-stream")
        stop, watcher = self._watch_cancel(connection)
        count = 0
        data_lines = []
        event_type = "message"
        event_id = None
        retry_ms = None
        event_size = 0
        try:
            for line, raw_size in self._iter_sse_lines(response, connection, deadline):
                event_size += raw_size
                if event_size > self.limits.max_body_bytes:
                    raise LimitExceeded("SSE event exceeds configured limit")
                try:
                    text = line.decode("utf-8")
                except UnicodeDecodeError:
                    raise ProtocolError("SSE stream is not valid UTF-8") from None
                validate_unicode(
                    text,
                    label="SSE line",
                    maximum=self.limits.max_line_bytes,
                    empty=True,
                    allow_text_newlines=True,
                )
                if text == "":
                    if data_lines:
                        count += 1
                        if count > self.limits.max_events:
                            raise LimitExceeded("SSE event count exceeds configured limit")
                        yield SseEvent(event_type, "\n".join(data_lines), event_id, retry_ms)
                    data_lines = []
                    event_type = "message"
                    retry_ms = None
                    event_size = 0
                    continue
                if text.startswith(":"):
                    continue
                field, separator, value = text.partition(":")
                if separator and value.startswith(" "):
                    value = value[1:]
                if field == "data":
                    data_lines.append(value)
                elif field == "event":
                    event_type = value or "message"
                elif field == "id" and "\x00" not in value:
                    event_id = value
                elif (
                    field == "retry"
                    and len(value) <= 7
                    and value.isascii()
                    and value.isdigit()
                ):
                    parsed_retry = int(value)
                    retry_ms = parsed_retry if parsed_retry <= 3_600_000 else None
            if data_lines:
                count += 1
                if count > self.limits.max_events:
                    raise LimitExceeded("SSE event count exceeds configured limit")
                yield SseEvent(event_type, "\n".join(data_lines), event_id, retry_ms)
        except (OSError, http.client.HTTPException) as exc:
            if self.cancellation.cancelled:
                self.cancellation.raise_if_cancelled()
            if isinstance(exc, (TimeoutError, socket.timeout)):
                raise TransportTimeout("loopback SSE operation timed out") from None
            raise TransportError("loopback SSE stream failed") from exc
        finally:
            stop.set()
            watcher.join(timeout=0.1)
            response.close()
            connection.close()

    async def aiter_sse(self, path: str, **kwargs: Any) -> AsyncIterator[SseEvent]:
        iterator = self.iter_sse(path, **kwargs)
        loop = asyncio.get_running_loop()

        def next_item() -> Tuple[bool, Optional[SseEvent]]:
            try:
                return True, next(iterator)
            except StopIteration:
                return False, None

        try:
            while True:
                present, item = await loop.run_in_executor(None, next_item)
                if not present:
                    return
                assert item is not None
                yield item
        except asyncio.CancelledError:
            self.cancellation.cancel()
            raise
        finally:
            try:
                iterator.close()
            except (RuntimeError, ValueError):
                pass


__all__ = ["HttpSseClient", "SseEvent"]
