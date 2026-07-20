"""OpenAI-compatible HTTP server unifying all three providers.

Run (localhost only — see the ToS warning below):
    pip install 'unified-cli[server]'
    python -m unified_cli.server --port 8000      # 127.0.0.1, bind-guarded
    # or: uvicorn unified_cli.server:app --port 8000

⚠️  This server runs on YOUR CLI subscription auth. Keep it on localhost.
    Exposing it to other people / networks (e.g. `--host 0.0.0.0`) routes their
    requests through your subscription and violates the providers' Terms of
    Service (account-ban risk). The `run()` / `python -m unified_cli.server`
    launcher refuses a non-loopback bind unless
    UNIFIED_CLI_ALLOW_EXTERNAL_BIND=1 and a strong
    UNIFIED_CLI_SERVER_AUTH_TOKEN are both set.

Model routing:
    "haiku" / "claude-*" / "sonnet" / "opus"     → Claude
    "gpt-*" / "o1-*" / "o3-*" / "codex-*"        → Codex
    "gemini-*"                                    → Gemini
    "claude/<m>" / "codex/<m>" / "gemini/<m>"    → explicit

Conversation persistence:
    The `user` field (OpenAI convention) is used as a conversation id.
    A UnifiedConversation is kept per conversation id, allowing multi-turn
    across providers with automatic context injection on switch.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import ipaddress
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Optional, Union

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import (
        HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse,
    )
    from pydantic import BaseModel
    from starlette.background import BackgroundTask
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "unified_cli.server requires 'fastapi', 'uvicorn', and 'pydantic'. "
        "Install with: pip install 'unified-cli[server]'"
    ) from e

from .conversation import UnifiedConversation
from .dashboard_tpl import DASHBOARD_HTML
from .errors import ErrorKind, UnifiedError
from .factory import _cannot_route_error, _route_builtin
from .i18n import t
from .models import list_models
from .ui import collect_states
from .usage import tracker


_log = logging.getLogger("unified_cli.server")

# Hosts that keep the server reachable only from this machine.
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_ALLOW_EXTERNAL_ENV = "UNIFIED_CLI_ALLOW_EXTERNAL_BIND"
_SERVER_AUTH_TOKEN_ENV = "UNIFIED_CLI_SERVER_AUTH_TOKEN"
_MIN_SERVER_AUTH_TOKEN_BYTES = 32

_PERSONAL_USE_NOTICE = (
    "unified-cli OpenAI-compat server started — personal / local use only. "
    "This server runs on YOUR CLI subscription auth; exposing it to other "
    "people or networks routes their requests through your subscription and "
    "violates the providers' Terms of Service (account-ban risk)."
)


def _positive_env(name: str, default: int) -> int:
    """Read a positive integer server limit without making startup fragile."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _log.warning("Ignoring invalid %s=%r; using %d", name, raw, default)
        return default
    if value < 1:
        _log.warning("Ignoring non-positive %s=%r; using %d", name, raw, default)
        return default
    return value


# HTTP-server limits are deliberately opt-in at this boundary. Direct Python
# API/CLI callers keep their existing attachment and history semantics.
_MAX_REQUEST_BODY_BYTES = _positive_env(
    "UNIFIED_CLI_SERVER_MAX_BODY_BYTES", 24 * 1024 * 1024)
_MAX_IMAGES = _positive_env("UNIFIED_CLI_SERVER_MAX_IMAGES", 4)
_MAX_IMAGE_BYTES = _positive_env(
    "UNIFIED_CLI_SERVER_MAX_IMAGE_BYTES", 4 * 1024 * 1024)
_MAX_PROMPT_CHARS = _positive_env(
    "UNIFIED_CLI_SERVER_MAX_PROMPT_CHARS", 256 * 1024)
_MAX_MESSAGES = _positive_env("UNIFIED_CLI_SERVER_MAX_MESSAGES", 64)
_MAX_USER_CHARS = _positive_env("UNIFIED_CLI_SERVER_MAX_USER_CHARS", 256)
_MAX_RESPONSE_CHARS = _positive_env(
    "UNIFIED_CLI_SERVER_MAX_RESPONSE_CHARS", 4 * 1024 * 1024)
_SERVER_HISTORY_TURNS = _positive_env(
    "UNIFIED_CLI_SERVER_HISTORY_TURNS", 8)
_SERVER_HISTORY_TURN_CHARS = _positive_env(
    "UNIFIED_CLI_SERVER_HISTORY_TURN_CHARS", 4 * 1024)
_SERVER_CLIENT_CACHE = _positive_env(
    "UNIFIED_CLI_SERVER_CLIENT_CACHE", 4)
_MAX_ACTIVE_TURNS = _positive_env(
    "UNIFIED_CLI_SERVER_MAX_ACTIVE_TURNS", 4)
_ALLOW_AGENTIC_SERVER_PROVIDERS_ENV = "UNIFIED_CLI_SERVER_ALLOW_AGENTIC_PROVIDERS"

# The HTTP server is a separate trust boundary from direct Python/CLI use. It
# accepts text from local clients, so it must not inherit a provider's broad
# agent tool surface, user configuration, plugins, or project rules. Claude is
# the only provider enabled by default: canonical data-image bytes create a
# scoped Read permission for that exact materialized file. Codex and agy are
# disabled unless the operator explicitly accepts their host-read risk in an
# external container/VM sandbox.
_SERVER_PROVIDER_OPTS: dict[str, dict] = {
    "claude": {
        "safe_mode": True,
        "permission_mode": "dontAsk",
        "tools": [],
        "restrict_image_reads": True,
    },
    "codex": {
        "ignore_user_config": True,
        "ignore_rules": True,
    },
    # Antigravity's current CLI remains agentic even with web_search=False.
    # It is rejected at the HTTP boundary unless the operator opts in below;
    # these flags are only a best-effort reduction for that explicit risk.
    "gemini": {
        "sandbox": True,
        "skip_permissions": False,
    },
}


def _agentic_server_providers_allowed() -> bool:
    return os.environ.get(_ALLOW_AGENTIC_SERVER_PROVIDERS_ENV, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _auth_token_env_is_set() -> bool:
    """Whether the operator explicitly requested bearer authentication.

    An empty or malformed value still counts as requested so a typo cannot
    silently turn an authenticated local server back into an open one.
    """
    return _SERVER_AUTH_TOKEN_ENV in os.environ


def _configured_server_auth_token() -> Optional[bytes]:
    """Return the configured bearer secret only when it meets the minimum.

    The value is deliberately kept as bytes for compare_digest(). Callers get
    only a boolean outcome; no path logs, formats, or returns the secret.
    """
    raw = os.environ.get(_SERVER_AUTH_TOKEN_ENV)
    if not raw or raw != raw.strip():
        return None
    value = raw.encode("utf-8")
    return value if len(value) >= _MIN_SERVER_AUTH_TOKEN_BYTES else None


def _server_auth_required() -> bool:
    """External opt-in requires auth; an explicitly configured token protects
    loopback traffic too, avoiding a surprising split trust boundary.
    """
    return _external_bind_allowed() or _auth_token_env_is_set()


def _require_server_auth_configuration() -> None:
    if not _server_auth_required() or _configured_server_auth_token() is not None:
        return
    raise UnifiedError(
        kind="config",
        provider="claude",
        message=(
            f"{_SERVER_AUTH_TOKEN_ENV} must be a non-whitespace bearer token "
            f"of at least {_MIN_SERVER_AUTH_TOKEN_BYTES} bytes."
        ),
        hint=(
            "Generate a new secret outside the command line, export it in the "
            "server environment, then send it as Authorization: Bearer <token>."
        ),
    )


def _valid_bearer_authorization(authorization: Optional[str]) -> bool:
    """Check one Authorization header without exposing timing or secret data."""
    expected = _configured_server_auth_token()
    if expected is None or not authorization:
        return False
    scheme, separator, supplied = authorization.partition(" ")
    if scheme != "Bearer" or not separator or not supplied or supplied != supplied.strip():
        return False
    try:
        supplied_bytes = supplied.encode("utf-8")
    except UnicodeError:  # pragma: no cover - Python strings normally encode
        return False
    return hmac.compare_digest(supplied_bytes, expected)


def _server_auth_config_error() -> "JSONResponse":
    """Fail closed for malformed/missing configured authentication state."""
    return JSONResponse(
        status_code=503,
        content={"error": {
            "message": "Server bearer authentication is not configured securely.",
            "type": "invalid_request_error",
            "code": "server_auth_unconfigured",
        }},
    )


def _server_auth_required_error() -> "JSONResponse":
    return JSONResponse(
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
        content={"error": {
            "message": "A valid bearer token is required for this server.",
            "type": "authentication_error",
            "code": "server_auth_required",
        }},
    )


def _body_too_large_payload() -> bytes:
    return json.dumps({
        "error": {
            "message": "Request body exceeds this local server's limit.",
            "type": "invalid_request_error",
            "code": "request_too_large",
        }
    }).encode("utf-8")


class _ChatBodyLimitMiddleware:
    """Bound the raw chat body before FastAPI/Pydantic parse it.

    A Content-Length check alone is bypassable with chunked transfer encoding.
    This ASGI-level middleware buffers at most the configured safe maximum and
    replays the body once to the downstream app; it never calls Request.body()
    before the limit is known.
    """

    def __init__(self, app, *, max_body_bytes: Optional[int] = None):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        if (scope.get("type") != "http"
                or scope.get("method") != "POST"
                or scope.get("path") != "/v1/chat/completions"):
            await self.app(scope, receive, send)
            return

        limit = self.max_body_bytes or _MAX_REQUEST_BODY_BYTES
        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                if int(value) > limit:
                    await self._send_too_large(send)
                    return
            except ValueError:
                # Let the ASGI server/FastAPI report malformed headers.
                pass
            break

        body = bytearray()
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                # A disconnect before a complete body is handled by FastAPI
                # through the replayed disconnect event.
                async def disconnected_receive():
                    return message
                await self.app(scope, disconnected_receive, send)
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > limit:
                await self._send_too_large(send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        delivered = False

        async def replay_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body),
                        "more_body": False}
            # Do not synthesize an immediate disconnect here: StreamingResponse
            # listens for disconnects after the request body is parsed, and a
            # fake one would cancel a perfectly healthy SSE response before its
            # generator is iterated. Delegate to the original ASGI receive for
            # a real client disconnect instead.
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _send_too_large(send) -> None:
        payload = _body_too_large_payload()
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": payload})


@asynccontextmanager
async def _lifespan(app: "FastAPI"):
    # Fires on every startup, including a direct `uvicorn ...:app` launch that
    # bypasses run()'s bind guard. Refuse startup when external access or an
    # explicitly requested local token would otherwise be unauthenticated.
    _require_server_auth_configuration()
    _log.warning(_PERSONAL_USE_NOTICE)
    yield


app = FastAPI(title="unified-cli OpenAI-compat", lifespan=_lifespan)
# Register before the decorator middleware below: Starlette inserts newly added
# middleware at the front, so the localhost guard remains the outermost check.
app.add_middleware(_ChatBodyLimitMiddleware)


def _is_loopback_host(host: str) -> bool:
    """True only for a genuine loopback host: the literal name "localhost", or
    an IP address that parses as loopback (127.0.0.0/8, ::1).

    Uses `ipaddress`, NOT a string prefix — `"127.".startswith` would wrongly
    accept an attacker DNS name like "127.evil.com" (DNS-rebinding bypass), and
    a trailing-dot FQDN like "127.0.0.1." — both are rejected here.
    """
    h = (host or "").strip().strip("[]")
    if h.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _host_header_name(host_header: str) -> str:
    """Extract the hostname from a Host header, dropping the port and IPv6
    brackets: '127.0.0.1:8000' → '127.0.0.1', '[::1]:8000' → '::1'."""
    h = (host_header or "").strip()
    if h.startswith("["):
        return h[1:h.index("]")] if "]" in h else h[1:]
    return h.rsplit(":", 1)[0] if ":" in h else h


@app.middleware("http")
async def _localhost_guard(request: "Request", call_next):
    """Enforce localhost-only at the ASGI layer, so the invariant holds even
    under a raw `uvicorn unified_cli.server:app --host 0.0.0.0` launch that
    bypasses run()'s bind guard. Rejects a non-loopback peer OR a non-loopback
    Host header (DNS-rebinding defense). External opt-in is bearer-protected;
    setting a bearer token intentionally protects loopback requests too."""
    if not _external_bind_allowed():
        client_host = request.client.host if request.client else ""
        host_name = _host_header_name(request.headers.get("host", ""))
        server_info = request.scope.get("server") or ("", 0)
        bound_host = server_info[0] if server_info else ""
        if (bound_host and not _is_loopback_host(bound_host)) or \
           (client_host and not _is_loopback_host(client_host)) or \
           (host_name and not _is_loopback_host(host_name)):
            return JSONResponse(
                status_code=403,
                content={"error": {
                    "message": ("This server is localhost-only. Set "
                                f"{_ALLOW_EXTERNAL_ENV}=1 to allow external access "
                                "(routes other people's requests through your "
                                "subscription — violates provider ToS)."),
                    "type": "invalid_request_error",
                    "code": "config",
                }},
            )

    if _server_auth_required():
        if _configured_server_auth_token() is None:
            return _server_auth_config_error()
        if not _valid_bearer_authorization(request.headers.get("authorization")):
            return _server_auth_required_error()
    return await call_next(request)


# Conversation ids map to slots rather than bare conversations so the LRU can
# never evict one while it is executing. All slot and active-turn mutation is
# protected by _CONVS_LOCK; a lease gives an endpoint exclusive ownership of a
# persisted conversation for its full send/SSE lifetime.
@dataclass
class _ConversationSlot:
    conv: UnifiedConversation
    active: bool = False


CONVS: "OrderedDict[str, _ConversationSlot]" = OrderedDict()
_CONVS_LOCK = threading.Lock()
_MAX_CONVS = _positive_env("UNIFIED_CLI_SERVER_MAX_CONVERSATIONS", 128)
_ACTIVE_TURNS = 0


class ChatMessage(BaseModel):
    """OpenAI-compatible message — content can be a plain string OR a list of
    content blocks (`{"type":"text", "text": ...}`, `{"type":"image_url",
    "image_url":{"url": "..."}}`).
    """
    role: str
    content: Union[str, list[dict]]


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    user: Optional[str] = None


# ---- error mapping ----

_ERROR_TO_STATUS: dict[ErrorKind, int] = {
    "auth_expired": 401,
    "rate_limit": 429,
    "model_not_allowed": 400,
    "config": 400,
    "not_found": 404,
    "network": 502,
    "resource_limit": 413,
    "internal": 500,
}

_ERROR_TO_TYPE: dict[ErrorKind, str] = {
    "auth_expired": "authentication_error",
    "rate_limit": "rate_limit_error",
    "model_not_allowed": "invalid_request_error",
    "config": "invalid_request_error",
    "not_found": "not_found_error",
    "network": "upstream_error",
    "resource_limit": "invalid_request_error",
    "internal": "internal_error",
}


def _openai_error(err: UnifiedError) -> dict:
    return {
        "error": {
            "message": err.message + (f" — {err.hint}" if err.hint else ""),
            "type": _ERROR_TO_TYPE[err.kind],
            "provider": err.provider,
            "code": err.kind,
        }
    }


def _stream_provider_error(message: str, provider: str) -> dict:
    """OpenAI-style SSE error for a structured provider stream event."""
    return {
        "error": {
            "message": message,
            "type": "upstream_error",
            "provider": provider,
            "code": "upstream_error",
        }
    }


def _raise_http(err: UnifiedError) -> None:
    raise HTTPException(
        status_code=_ERROR_TO_STATUS[err.kind],
        detail=_openai_error(err)["error"],
    )


# ---- helpers ----

def _raise_request_error(
    message: str,
    *,
    code: str = "invalid_request",
    status_code: int = 400,
    retry_after: Optional[int] = None,
) -> None:
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    raise HTTPException(
        status_code=status_code,
        detail={
            "message": message,
            "type": "invalid_request_error",
            "code": code,
        },
        headers=headers,
    )


_DATA_IMAGE_RE = re.compile(
    r"data:(image/(?:png|jpeg|gif|webp));base64,([A-Za-z0-9+/]+={0,2})",
    re.ASCII,
)


def _valid_image_signature(media_type: str, data: bytes) -> bool:
    if media_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if media_type == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if media_type == "image/webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    return False  # pragma: no cover - regex constrains the accepted values


def _decode_server_data_image(url: Any):
    """Decode one canonical image data URI at the HTTP trust boundary.

    Never hand untrusted path/URL strings to normalize_image(): the direct
    Python API intentionally supports local paths, but the HTTP endpoint must
    not turn a request into an agentic local-file read.
    """
    if not isinstance(url, str):
        _raise_request_error("image_url.url must be a string.", code="invalid_image")
    match = _DATA_IMAGE_RE.fullmatch(url)
    if not match:
        _raise_request_error(
            "image_url must be a base64 data:image/png, jpeg, gif, or webp URI; "
            "remote URLs and filesystem paths are not accepted by this server.",
            code="invalid_image",
        )
    media_type, encoded = match.groups()
    max_encoded = 4 * ((_MAX_IMAGE_BYTES + 2) // 3)
    if len(encoded) > max_encoded:
        _raise_request_error(
            "image_url exceeds the server image-size limit.",
            code="image_too_large", status_code=413,
        )
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        _raise_request_error("image_url has invalid base64 data.", code="invalid_image")
    if not data:
        _raise_request_error("image_url image data is empty.", code="invalid_image")
    if len(data) > _MAX_IMAGE_BYTES:
        _raise_request_error(
            "image_url exceeds the server image-size limit.",
            code="image_too_large", status_code=413,
        )
    if not _valid_image_signature(media_type, data):
        _raise_request_error(
            "image_url MIME type does not match its image data.",
            code="invalid_image",
        )
    from .core import Attachment
    return Attachment(bytes_=data, media_type=media_type)


def _validate_chat_request(req: ChatRequest) -> None:
    if not req.model or not req.model.strip():
        _raise_request_error("model must not be empty.")
    if len(req.model) > 256:
        _raise_request_error("model exceeds the server limit.", code="model_too_long")
    if not req.messages:
        _raise_request_error("messages must not be empty.")
    if len(req.messages) > _MAX_MESSAGES:
        _raise_request_error(
            "messages exceeds the server limit.",
            code="too_many_messages", status_code=413,
        )
    if req.user is not None and len(req.user) > _MAX_USER_CHARS:
        _raise_request_error(
            "user exceeds the server conversation-id limit.",
            code="user_too_long", status_code=413,
        )

    text_chars = 0
    for message in req.messages:
        if not message.role or len(message.role) > 32:
            _raise_request_error("message role is invalid.")
        if isinstance(message.content, str):
            text_chars += len(message.content)
        else:
            for block in message.content:
                if block.get("type") == "text":
                    text = block.get("text", "")
                    if not isinstance(text, str):
                        _raise_request_error("text content must be a string.")
                    text_chars += len(text)
        if text_chars > _MAX_PROMPT_CHARS:
            _raise_request_error(
                "prompt text exceeds the server limit.",
                code="prompt_too_large", status_code=413,
            )

def _extract_user_message(messages: list[ChatMessage]) -> tuple[str, list[Any]]:
    """Return (prompt_text, images) from the last user message.

    Supports both plain string content and OpenAI multi-content arrays:
        content = "hello"
        content = [{"type":"text","text":"..."}, {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}]

    At the HTTP boundary, image blocks must be canonical base64 `data:image`
    values. The direct Python API remains the place to use trusted local paths
    or explicitly downloaded image bytes.
    """
    for m in reversed(messages):
        if m.role != "user":
            continue
        if isinstance(m.content, str):
            return m.content, []
        # multi-content list
        text_parts: list[str] = []
        images: list[Any] = []
        for block in m.content:
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if not isinstance(text, str):
                    _raise_request_error("text content must be a string.")
                text_parts.append(text)
            elif btype == "image_url":
                image_url = block.get("image_url")
                if not isinstance(image_url, dict):
                    _raise_request_error(
                        "image_url must be an object containing a url.",
                        code="invalid_image",
                    )
                url = image_url.get("url", "")
                if len(images) >= _MAX_IMAGES:
                    _raise_request_error(
                        "too many images in one message.",
                        code="too_many_images", status_code=413,
                    )
                images.append(_decode_server_data_image(url))
        return "\n".join(p for p in text_parts if p).strip(), images
    _raise_request_error("no user message")


def _new_server_conversation() -> UnifiedConversation:
    return UnifiedConversation(
        sticky=False,
        # No server request gets web search or a default agent tool surface.
        # Direct Python and terminal CLI calls retain their existing defaults.
        provider_opts={"web_search": False},
        provider_opts_by_provider=_SERVER_PROVIDER_OPTS,
        max_turns=_SERVER_HISTORY_TURNS,
        max_turn_chars=_SERVER_HISTORY_TURN_CHARS,
        max_clients=_SERVER_CLIENT_CACHE,
    )


def _evict_idle_until_room_locked() -> bool:
    """Make room in the persistent LRU without ever evicting an active turn."""
    while len(CONVS) >= _MAX_CONVS:
        victim = next((key for key, slot in CONVS.items() if not slot.active), None)
        if victim is None:
            return False
        del CONVS[victim]
    return True


def _get_conv(conv_id: str) -> UnifiedConversation:
    """Fetch/create an *idle* stored conversation (legacy helper/tests)."""
    with _CONVS_LOCK:
        slot = CONVS.get(conv_id)
        if slot is None:
            if not _evict_idle_until_room_locked():
                raise RuntimeError("all server conversation slots are active")
            slot = _ConversationSlot(_new_server_conversation())
            CONVS[conv_id] = slot
        else:
            CONVS.move_to_end(conv_id)
        return slot.conv


@dataclass
class _ConversationLease:
    conv_id: str
    conv: UnifiedConversation
    slot: Optional[_ConversationSlot] = None
    _released: bool = False

    def release(self) -> None:
        global _ACTIVE_TURNS
        with _CONVS_LOCK:
            if self._released:
                return
            self._released = True
            if self.slot is not None:
                self.slot.active = False
            _ACTIVE_TURNS = max(0, _ACTIVE_TURNS - 1)


def _acquire_conversation(user: Optional[str]) -> _ConversationLease:
    """Reserve an idle conversation now; do not queue mutable turns."""
    global _ACTIVE_TURNS
    conv_id = user or str(uuid.uuid4())
    with _CONVS_LOCK:
        slot: Optional[_ConversationSlot] = None
        if user:
            slot = CONVS.get(conv_id)
            if slot is not None and slot.active:
                _raise_request_error(
                    "A request for this conversation is already in progress.",
                    code="conversation_busy", status_code=409,
                )
        if _ACTIVE_TURNS >= _MAX_ACTIVE_TURNS:
            _raise_request_error(
                "This local server is busy; retry shortly.",
                code="server_busy", status_code=429, retry_after=1,
            )
        if user:
            if slot is None:
                if not _evict_idle_until_room_locked():
                    _raise_request_error(
                        "All persistent conversation slots are active.",
                        code="conversation_capacity", status_code=503,
                        retry_after=1,
                    )
                slot = _ConversationSlot(_new_server_conversation())
                CONVS[conv_id] = slot
            else:
                CONVS.move_to_end(conv_id)
            slot.active = True
            conv = slot.conv
        else:
            conv = _new_server_conversation()
        _ACTIVE_TURNS += 1
        return _ConversationLease(conv_id=conv_id, conv=conv, slot=slot)


# ---- endpoints ----

@app.post("/v1/chat/completions")
def chat_completions(req: ChatRequest):
    _validate_chat_request(req)
    # The /v1 boundary accepts only Core routing. This pure lookup preserves
    # historical Core inference (including slash-containing vendor model ids)
    # while ensuring extension metadata is never discovered or loaded here.
    try:
        routed = _route_builtin(req.model)
        if routed is None:
            raise _cannot_route_error(req.model)
        provider, model = routed
    except UnifiedError as e:
        _raise_http(e)
    if (provider in ("codex", "gemini")
            and not _agentic_server_providers_allowed()):
        provider_name = "Codex" if provider == "codex" else "agy (Antigravity)"
        _raise_request_error(
            f"{provider_name} is disabled for the local HTTP server because "
            "this wrapper cannot provide confidential-data isolation for its "
            "agentic file/tool surface. Use the direct CLI/Python API, or set "
            f"{_ALLOW_AGENTIC_SERVER_PROVIDERS_ENV}=1 only inside an externally "
            "sandboxed container or VM with an intentional workspace mount. "
            "That opt-in is not a general safety switch.",
            code="provider_disabled_for_server",
            status_code=403,
        )

    prompt, images = _extract_user_message(req.messages)
    images = images or None
    # Reserve now rather than queueing mutable conversation turns. A second
    # request with the same user id receives a clear 409 while the first owns
    # its native provider session; anonymous requests count toward capacity.
    lease = _acquire_conversation(req.user)
    conv_id = lease.conv_id
    conv = lease.conv

    response_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"
    created = int(time.time())

    if req.stream:
        def gen():
            upstream = None
            finish_reason = "stop"
            try:
                upstream = conv.stream(prompt, provider=provider, model=model,
                                       images=images)
                sent_chars = 0
                for msg in upstream:
                    if msg.kind == "text" and msg.text:
                        remaining = _MAX_RESPONSE_CHARS - sent_chars
                        if remaining <= 0:
                            finish_reason = "length"
                            break
                        text = msg.text[:remaining]
                        sent_chars += len(text)
                        chunk = {
                            "id": response_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": req.model,
                            "choices": [{
                                "index": 0,
                                "delta": {"content": text},
                                "finish_reason": None,
                            }],
                        }
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                        if len(text) < len(msg.text):
                            finish_reason = "length"
                            break
                    elif msg.kind == "error":
                        error = _stream_provider_error(
                            msg.error or "Upstream provider returned an error.",
                            provider,
                        )
                        yield f"data: {json.dumps(error, ensure_ascii=False)}\n\n"
                        return
            except UnifiedError as e:
                yield f"data: {json.dumps(_openai_error(e), ensure_ascii=False)}\n\n"
                return
            finally:
                # Closing on a response ceiling or client disconnect lets the
                # provider's generator terminate its child process promptly.
                try:
                    if upstream is not None:
                        close = getattr(upstream, "close", None)
                        if close is not None:
                            close()
                finally:
                    lease.release()

            final = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": req.model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            }
            yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        # Covers the narrow case where ASGI abandons the response before
        # iteration starts. release() is deliberately idempotent.
        return StreamingResponse(
            gen(), media_type="text/event-stream",
            background=BackgroundTask(lease.release),
        )

    try:
        resp = conv.send(prompt, provider=provider, model=model, images=images)
    except UnifiedError as e:
        _raise_http(e)
    finally:
        lease.release()

    response_text = resp.text
    finish_reason = "stop"
    if len(response_text) > _MAX_RESPONSE_CHARS:
        response_text = response_text[:_MAX_RESPONSE_CHARS]
        finish_reason = "length"

    return {
        "id": response_id,
        "object": "chat.completion",
        "created": created,
        "model": req.model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": response_text},
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": resp.usage.input_tokens or 0,
            "completion_tokens": resp.usage.output_tokens or 0,
            "total_tokens": (resp.usage.total_tokens
                             or (resp.usage.input_tokens or 0)
                             + (resp.usage.output_tokens or 0)),
        },
        "x_conversation_id": conv_id,
        "x_provider": resp.provider,
        "x_session_id": resp.session_id,
    }


@app.get("/v1/models")
def list_all_models(provider: Optional[str] = None):
    providers = ("claude", "codex", "gemini")
    if provider is not None and provider not in providers:
        _raise_request_error(
            "provider must be one of: claude, codex, gemini",
            code="invalid_provider",
        )

    # Do not make disabled agentic providers discoverable (or trigger their
    # potentially network-backed model discovery) through the local server.
    # Direct CLI/Python use remains unaffected by this HTTP-only policy.
    enabled = providers if _agentic_server_providers_allowed() else ("claude",)
    if provider is not None:
        mods = list_models(provider) if provider in enabled else []
    else:
        mods = [model for name in enabled for model in list_models(name)]
    return {
        "object": "list",
        "data": [
            {
                "id": m.id,
                "object": "model",
                "owned_by": m.provider,
                "default": m.default,
                "source": m.source,
            } for m in mods
        ],
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/v1/doctor")
def doctor_endpoint():
    """Provider health/state snapshot (drives /dashboard)."""
    return [
        {
            "provider": s.name,
            "bin_path": s.bin_path,
            "has_oauth": s.has_oauth,
            "has_api_key": s.has_api_key,
            "has_token_env": s.has_token_env,
            "keychain": s.keychain,
            "api_key_env": s.api_key_env,
            "model_count": s.model_count,
            "model_source": s.model_source,
            "default_model": s.default_model,
            "health": s.health,
        }
        for s in collect_states()
    ]


@app.get("/v1/usage")
def usage_endpoint():
    """Process-lifetime usage aggregates + recent calls."""
    return tracker.snapshot()


@app.get("/v1/conversations")
def conversations_endpoint():
    """Active UnifiedConversations tracked by this server."""
    out = []
    # Snapshot slots while protected. Active slots are intentionally reported
    # without peeking at mutable turn/session state; their owner may be in the
    # middle of a provider stream.
    with _CONVS_LOCK:
        for conv_id, slot in CONVS.items():
            if slot.active:
                out.append({"conversation_id": conv_id, "active": True})
                continue
            conv = slot.conv
            out.append({
                "conversation_id": conv_id,
                "active": False,
                "last_provider": conv.turns[-1].provider if conv.turns else None,
                "turn_count": len(conv.turns),
                "sessions": dict(conv.sessions),
            })
    return {"conversations": out}


@app.get("/")
def root():
    """Send the bare host to the dashboard so `http://127.0.0.1:PORT/` just works."""
    return RedirectResponse(url="/dashboard")


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    """Browser dashboard (loopback by default; bearer-guarded when configured)."""
    return HTMLResponse(DASHBOARD_HTML)


# ---- launcher with a localhost guard ----

def _external_bind_allowed() -> bool:
    return os.environ.get(_ALLOW_EXTERNAL_ENV, "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def run(host: str = "127.0.0.1", port: int = 8000, **uvicorn_kwargs) -> None:
    """Launch the OpenAI-compatible server.

    Binds to loopback (127.0.0.1) by default. Binding to a non-loopback host
    exposes a server that runs on YOUR subscription auth — anyone who can reach
    it has their requests served by your Pro/Max (or agy) account, which
    violates the providers' Terms of Service and risks an account ban. We
    therefore REFUSE a non-loopback bind unless you explicitly opt in with
    ``UNIFIED_CLI_ALLOW_EXTERNAL_BIND=1`` and set a strong
    ``UNIFIED_CLI_SERVER_AUTH_TOKEN`` (not recommended).

    A configured bearer token is also enforced on loopback, so users can opt
    into local process-to-process authentication without a separate mode.
    """
    if not _is_loopback_host(host):
        warning = t("server.external_bind.warning", host=host)
        if not _external_bind_allowed():
            raise UnifiedError(
                kind="config", provider="claude",
                message=warning.strip(),
                # `{env}` substitutes the literal env var name so the opt-in
                # hint always names UNIFIED_CLI_ALLOW_EXTERNAL_BIND (test_gate).
                hint=t("server.external_bind.hint", env=_ALLOW_EXTERNAL_ENV),
            )
        print(warning + t("server.external_bind.proceeding", env=_ALLOW_EXTERNAL_ENV),
              file=sys.stderr)

    # Validate before importing/running uvicorn so both the explicit launcher
    # and raw-ASGI lifespan path fail closed for a missing or weak secret.
    _require_server_auth_configuration()

    import uvicorn

    uvicorn.run(app, host=host, port=port, **uvicorn_kwargs)


if __name__ == "__main__":
    # `python -m unified_cli.server` → localhost-guarded launch.
    import argparse

    ap = argparse.ArgumentParser(prog="unified_cli.server",
                                 description="unified-cli OpenAI-compatible server (localhost by default)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    _args = ap.parse_args()
    try:
        run(host=_args.host, port=_args.port)
    except UnifiedError as _e:
        print(f"{_e.message}\n{_e.hint or ''}", file=sys.stderr)
        sys.exit(2)
