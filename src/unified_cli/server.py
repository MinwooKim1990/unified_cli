"""OpenAI-compatible HTTP server unifying all three providers.

Run:
    pip install '.[server]'
    uvicorn unified_cli.server:app --port 8000

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
import time
import uuid
from typing import Optional

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


app = FastAPI(title="unified-cli OpenAI-compat")

# conversation-id → UnifiedConversation (sticky=False so providers can mix)
CONVS: dict[str, UnifiedConversation] = {}


class ChatMessage(BaseModel):
    role: str
    content: str


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

def _last_user(messages: list[ChatMessage]) -> str:
    for m in reversed(messages):
        if m.role == "user":
            return m.content
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

    prompt = _last_user(req.messages)
    conv_id = req.user or str(uuid.uuid4())
    conv = _get_conv(conv_id)

    response_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"
    created = int(time.time())

    if req.stream:
        def gen():
            try:
                for msg in conv.stream(prompt, provider=provider, model=model):
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
        resp = conv.send(prompt, provider=provider, model=model)
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
