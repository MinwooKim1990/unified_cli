#!/usr/bin/env python3
"""Deterministic stand-in for the three wrapped CLIs.

The characterization tests copy this file to a temporary executable. Behaviour
is selected through ``FAKE_PROVIDER`` / ``FAKE_RESPONSE`` so no installed CLI,
login, network access, or user configuration participates in the tests.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def emit(value: dict) -> None:
    print(json.dumps(value), flush=True)


def capture() -> None:
    path = os.environ.get("FAKE_CAPTURE")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as file:
        file.write(json.dumps({
            "argv": sys.argv[1:],
            "stdin": sys.stdin.read(),
        }) + "\n")


def claude(response: str, session: str) -> None:
    if response == "exit_auth":
        print("authentication_error: OAuth token has expired", file=sys.stderr)
        raise SystemExit(1)
    if response == "is_error":
        emit({"type": "result", "is_error": True, "result": "policy denied"})
        return

    args = sys.argv[1:]
    streaming = "stream-json" in args
    if not streaming:
        emit({
            "type": "result",
            "result": "hello from claude",
            "session_id": session,
            "usage": {
                "input_tokens": 11,
                "output_tokens": 5,
                "cache_read_input_tokens": 2,
            },
            "modelUsage": {},
            "is_error": False,
        })
        return

    emit({"type": "system", "session_id": session})
    emit({
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "plan"},
        },
    })
    emit({
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "hel"},
        },
    })
    emit({
        "type": "assistant",
        "message": {"content": [
            {"type": "thinking", "thinking": "plan more"},
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "name": "Read", "input": {"path": "x"},
             "id": "tool-1"},
        ]},
    })
    emit({
        "type": "result",
        "result": "hello",
        "session_id": session,
        "usage": {
            "input_tokens": 11,
            "output_tokens": 5,
            "cache_read_input_tokens": 2,
        },
        "is_error": False,
    })


def codex(response: str, session: str) -> None:
    if response == "exit_auth":
        print("401 Unauthorized", file=sys.stderr)
        raise SystemExit(1)
    if response == "structured_error":
        emit({"type": "turn.failed", "error": {"message": "turn exploded"}})
        return

    emit({"type": "thread.started", "thread_id": session})
    emit({"type": "item.completed", "item": {
        "type": "reasoning", "id": "reason-1", "text": "inspect",
    }})
    emit({"type": "item.completed", "item": {
        "type": "web_search", "id": "search-1",
        "action": {"query": "fixture"},
    }})
    emit({"type": "item.completed", "item": {
        "type": "function_call", "id": "call-1", "name": "lookup",
        "arguments": {"key": "value"},
    }})
    emit({"type": "item.completed", "item": {
        "type": "command_execution", "id": "call-1", "output": "ok",
    }})
    emit({"type": "item.completed", "item": {
        "type": "agent_message", "id": "message-1", "text": "hello from codex",
    }})
    emit({"type": "turn.completed", "usage": {
        "input_tokens": 13,
        "output_tokens": 7,
        "cached_input_tokens": 3,
    }})


def gemini(response: str, session: str) -> None:
    if response == "exit_auth":
        print("No refresh token is set", file=sys.stderr)
        raise SystemExit(1)
    if response == "ineligible":
        print("IneligibleTierError: no longer supported for Gemini Code Assist")
        return

    conv_dir = os.environ.get("FAKE_CONVERSATIONS_DIR")
    if conv_dir:
        path = Path(conv_dir)
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{session}.db").touch()
    print("hello from agy", flush=True)
    print("second line", flush=True)


capture()
provider = os.environ["FAKE_PROVIDER"]
response = os.environ.get("FAKE_RESPONSE", "ok")
session = os.environ.get("FAKE_SESSION", f"{provider}-session")
{"claude": claude, "codex": codex, "gemini": gemini}[provider](response, session)
