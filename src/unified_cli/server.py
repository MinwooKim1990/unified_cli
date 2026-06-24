"""OpenAI-compatible HTTP server unifying all three providers.

Run (localhost only — see the ToS warning below):
    pip install 'unified-cli[server]'
    python -m unified_cli.server --port 8000      # 127.0.0.1, bind-guarded
    # or: uvicorn unified_cli.server:app --port 8000

⚠️  This server runs on YOUR CLI subscription auth and has no built-in auth or
    rate limiting. Keep it on localhost. Exposing it to other people / networks
    (e.g. `--host 0.0.0.0`) routes their requests through your subscription and
    violates the providers' Terms of Service (account-ban risk). The `run()` /
    `python -m unified_cli.server` launcher refuses a non-loopback bind unless
    UNIFIED_CLI_ALLOW_EXTERNAL_BIND=1 is set.

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

import json
import logging
import os
import sys
import time
import uuid
import base64 as _b64
from contextlib import asynccontextmanager
from typing import Any, Optional, Union

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse, StreamingResponse
    from pydantic import BaseModel
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "unified_cli.server requires 'fastapi', 'uvicorn', and 'pydantic'. "
        "Install with: pip install '.[server]'"
    ) from e

from .conversation import UnifiedConversation
from .dashboard_tpl import DASHBOARD_HTML
from .errors import ErrorKind, UnifiedError
from .factory import route
from .models import list_models
from .ui import collect_states
from .usage import tracker


_log = logging.getLogger("unified_cli.server")

# Hosts that keep the server reachable only from this machine.
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_ALLOW_EXTERNAL_ENV = "UNIFIED_CLI_ALLOW_EXTERNAL_BIND"

_PERSONAL_USE_NOTICE = (
    "unified-cli OpenAI-compat server started — personal / local use only. "
    "This server runs on YOUR CLI subscription auth; exposing it to other "
    "people or networks routes their requests through your subscription and "
    "violates the providers' Terms of Service (account-ban risk)."
)


@asynccontextmanager
async def _lifespan(app: "FastAPI"):
    # Fires on every startup, including a direct `uvicorn ...:app` launch that
    # bypasses run()'s bind guard — so the reminder is always shown.
    _log.warning(_PERSONAL_USE_NOTICE)
    yield


app = FastAPI(title="unified-cli OpenAI-compat", lifespan=_lifespan)

# conversation-id → UnifiedConversation (sticky=False so providers can mix)
CONVS: dict[str, UnifiedConversation] = {}


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
    "internal": 500,
}

_ERROR_TO_TYPE: dict[ErrorKind, str] = {
    "auth_expired": "authentication_error",
    "rate_limit": "rate_limit_error",
    "model_not_allowed": "invalid_request_error",
    "config": "invalid_request_error",
    "not_found": "not_found_error",
    "network": "upstream_error",
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


def _raise_http(err: UnifiedError) -> None:
    raise HTTPException(
        status_code=_ERROR_TO_STATUS[err.kind],
        detail=_openai_error(err)["error"],
    )


# ---- helpers ----

def _extract_user_message(messages: list[ChatMessage]) -> tuple[str, list[Any]]:
    """Return (prompt_text, images) from the last user message.

    Supports both plain string content and OpenAI multi-content arrays:
        content = "hello"
        content = [{"type":"text","text":"..."}, {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}]

    `image_url` URLs may be either `data:` (base64) or http(s)://. Both forms
    are passed to the wrapper as-is — the provider layer will materialize
    bytes to disk if a CLI needs a path.
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
                text_parts.append(block.get("text", ""))
            elif btype == "image_url":
                url = (block.get("image_url") or {}).get("url", "")
                if not url:
                    continue
                if url.startswith("data:"):
                    # data:image/png;base64,XXXX → decode to bytes
                    head, _, b64 = url.partition(",")
                    media_type = head[5:].split(";", 1)[0] or None
                    try:
                        from .core import Attachment
                        images.append(Attachment(
                            bytes_=_b64.b64decode(b64),
                            media_type=media_type,
                        ))
                    except Exception:
                        pass
                else:
                    images.append(url)   # http(s) URL — passed as-is
        return "\n".join(p for p in text_parts if p).strip(), images
    raise HTTPException(status_code=400, detail="no user message")


def _get_conv(conv_id: str) -> UnifiedConversation:
    if conv_id not in CONVS:
        CONVS[conv_id] = UnifiedConversation(sticky=False)
    return CONVS[conv_id]


# ---- endpoints ----

@app.post("/v1/chat/completions")
def chat_completions(req: ChatRequest):
    try:
        provider, model = route(req.model)
    except UnifiedError as e:
        _raise_http(e)

    prompt, images = _extract_user_message(req.messages)
    images = images or None
    conv_id = req.user or str(uuid.uuid4())
    conv = _get_conv(conv_id)

    response_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"
    created = int(time.time())

    if req.stream:
        def gen():
            try:
                for msg in conv.stream(prompt, provider=provider, model=model,
                                        images=images):
                    if msg.kind == "text" and msg.text:
                        chunk = {
                            "id": response_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": req.model,
                            "choices": [{
                                "index": 0,
                                "delta": {"content": msg.text},
                                "finish_reason": None,
                            }],
                        }
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            except UnifiedError as e:
                yield f"data: {json.dumps(_openai_error(e), ensure_ascii=False)}\n\n"
                return

            final = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": req.model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    try:
        resp = conv.send(prompt, provider=provider, model=model, images=images)
    except UnifiedError as e:
        _raise_http(e)

    return {
        "id": response_id,
        "object": "chat.completion",
        "created": created,
        "model": req.model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": resp.text},
            "finish_reason": "stop",
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
    mods = list_models(provider)  # type: ignore[arg-type]
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
    for conv_id, conv in CONVS.items():
        last_provider = conv.turns[-1].provider if conv.turns else None
        out.append({
            "conversation_id": conv_id,
            "last_provider": last_provider,
            "turn_count": len(conv.turns),
            "sessions": dict(conv.sessions),
        })
    return {"conversations": out}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    """Browser dashboard (localhost only — no auth)."""
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
    ``UNIFIED_CLI_ALLOW_EXTERNAL_BIND=1`` (not recommended).

    There is no built-in auth or rate limiting; keep it on localhost.
    """
    import uvicorn

    if host not in _LOOPBACK:
        warning = (
            f"\n⚠️  비-로컬 호스트({host})에 바인딩하려 합니다.\n"
            "    이 서버는 당신의 구독 인증으로 동작합니다 — 외부에서 접근하면\n"
            "    타인의 요청이 당신 구독으로 처리되어 각 제공자 ToS 위반(계정\n"
            "    정지/차단 위험)이 됩니다. 개인 로컬 용도로만 쓰세요.\n"
        )
        if not _external_bind_allowed():
            raise UnifiedError(
                kind="config", provider="claude",
                message=warning.strip(),
                hint=(
                    f"정말 외부 노출이 필요하면 {_ALLOW_EXTERNAL_ENV}=1 을 "
                    "설정하세요 (권장하지 않음)."
                ),
            )
        print(warning + f"    {_ALLOW_EXTERNAL_ENV}=1 설정됨 — 본인 책임 하에 진행합니다.\n",
              file=sys.stderr)

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
