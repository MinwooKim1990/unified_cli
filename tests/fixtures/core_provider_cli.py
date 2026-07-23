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


def capture() -> int:
    path = os.environ.get("FAKE_CAPTURE")
    if not path:
        return 1
    with open(path, "a", encoding="utf-8") as file:
        file.write(json.dumps({
            "argv": sys.argv[1:],
            "stdin": sys.stdin.read(),
        }) + "\n")
    with open(path, encoding="utf-8") as file:
        return sum(1 for line in file if line.strip())


def fail_before_turn(response: str, attempt: int) -> None:
    if response == "transient_network" and attempt < 3:
        print("ENOTFOUND: DNS resolution failed", file=sys.stderr)
        raise SystemExit(1)
    retry_after = {
        "retry_after_valid": "2",
        "retry_after_invalid": "not-a-delay",
        "retry_after_capped": "999",
        "retry_after_repeated": "999",
    }.get(response)
    if retry_after is not None and (
        attempt == 1 or (response == "retry_after_repeated" and attempt < 3)
    ):
        print("429 Too Many Requests", file=sys.stderr)
        print(f"Retry-After: {retry_after}", file=sys.stderr)
        raise SystemExit(1)
    permanent = {
        "exit_401": "401 Unauthorized",
        "exit_403": "403 Forbidden",
        "quota": "429 insufficient_quota: quota exceeded",
        "policy": "429 request blocked by content policy",
        "plan_denial": "429 your plan does not include this model",
        "entitlement_denial_multiline": (
            "HTTP/1.1 429 Too Many Requests\n"
            "detail: account entitlement is exhausted"
        ),
    }.get(response)
    if permanent is not None:
        print(permanent, file=sys.stderr)
        raise SystemExit(1)
    if response == "plan_denial_json":
        print(json.dumps({
            "error": {"message": "upgrade subscription to continue"},
            "status": 429,
        }, indent=2), file=sys.stderr)
        raise SystemExit(1)
    if response == "incomplete_ninth_json_429":
        for _ in range(8):
            print(json.dumps({"error": {"message": "server busy"}}),
                  file=sys.stderr)
        print(json.dumps({
            "status": 429, "error": {"message": "server busy"},
        }), file=sys.stderr)
        raise SystemExit(1)
    permanent_variant = {
        "account_disabled_denial": (
            "HTTP status 429: account has been disabled."
        ),
        "payment_required_denial": (
            "status_code=429 payment: required."
        ),
        "normalized_billing_denial": (
            "HTTP_STATUS_CODE=429 billing's been disabled for your account"
        ),
        "normalized_payment_denial": (
            "HTTP status code: 429 payment—required"
        ),
        "normalized_plan_denial": (
            "status-code=429 plan isn't supported"
        ),
        "slash_normalized_denial": (
            "ERROR/HTTP/status-code/429 plan isn't supported"
        ),
    }.get(response)
    if permanent_variant is not None:
        print(permanent_variant, file=sys.stderr)
        raise SystemExit(1)


def emit_tool_then_fail(provider: str, error: str) -> None:
    if provider == "claude":
        emit({
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use", "name": "Write", "input": {"path": "x"},
                "id": "tool-side-effect",
            }]},
        })
    elif provider == "codex":
        emit({
            "type": "item.completed",
            "item": {"type": "function_call", "name": "write_file", "id": "call-1"},
        })
    else:
        print("tool use started: write_file", flush=True)
    print(error, file=sys.stderr)
    raise SystemExit(1)


def fail_with_adversarial_evidence(response: str) -> None:
    if response == "raw_error_event_then_429":
        emit({
            "type": "error", "status": 429,
            "error": {"message": "public error event"},
        })
    elif response == "stderr_partial_then_429":
        print(json.dumps({
            "assistant": {"content": "partial output"},
        }), file=sys.stderr)
    elif response == "deep_json_then_429":
        sys.stdout.write('{"error":' + '[' * 2000 + '0' + ']' * 2000 + '}\n')
        sys.stdout.flush()
    else:
        return
    print("429 Too Many Requests", file=sys.stderr)
    print("Retry-After: 1", file=sys.stderr)
    raise SystemExit(1)


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


provider = os.environ["FAKE_PROVIDER"]
response = os.environ.get("FAKE_RESPONSE", "ok")
session = os.environ.get("FAKE_SESSION", f"{provider}-session")
attempt = capture()
fail_before_turn(response, attempt)
fail_with_adversarial_evidence(response)
if response in {"tool_then_failure", "tool_then_429"}:
    failure = (
        "429 Too Many Requests\nRetry-After: 1"
        if response == "tool_then_429"
        else "ENOTFOUND: connection failed after tool execution"
    )
    emit_tool_then_fail(provider, failure)
{"claude": claude, "codex": codex, "gemini": gemini}[provider](response, session)
