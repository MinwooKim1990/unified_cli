"""Terminal CLI: `unified-cli {chat,stream,models,doctor}`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __name__ as _pkg_name  # noqa
from .conversation import UnifiedConversation
from .discovery import FINDERS
from .errors import UnifiedError
from .factory import create, route
from .models import DEFAULT_MODELS, list_models


def _cmd_doctor(_: argparse.Namespace) -> int:
    print("=== unified-cli doctor ===\n")

    # binaries
    for name, finder in FINDERS.items():
        path = finder()
        mark = "✓" if path else "✗"
        print(f"[{mark}] {name}: {path or '(not found)'}")

    # auth files
    home = Path.home()
    auth_files = {
        "claude": home / ".claude" / ".credentials.json",
        "codex":  home / ".codex"  / "auth.json",
        "gemini": home / ".gemini" / "oauth_creds.json",
    }
    print("\n--- auth ---")
    for name, p in auth_files.items():
        api_key_env = {"claude": "ANTHROPIC_API_KEY",
                       "codex": "OPENAI_API_KEY",
                       "gemini": "GEMINI_API_KEY"}[name]
        has_oauth = p.exists()
        has_key = api_key_env in os.environ
        status = []
        if has_oauth: status.append("OAuth")
        if has_key:   status.append(f"${api_key_env}")
        if not status: status = ["(none)"]
        print(f"  {name}: {' + '.join(status)}")

    # model counts
    print("\n--- models ---")
    for name in FINDERS:
        try:
            mods = list_models(name)  # type: ignore[arg-type]
            srcs = {m.source for m in mods}
            print(f"  {name}: {len(mods)} models  (source: {','.join(sorted(srcs))})  "
                  f"default={DEFAULT_MODELS[name]}")
        except Exception as e:
            print(f"  {name}: ERROR {e}")

    return 0


def _cmd_models(args: argparse.Namespace) -> int:
    mods = list_models(args.provider, force_refresh=args.refresh)
    if args.json:
        print(json.dumps(
            [{"id": m.id, "provider": m.provider, "display_name": m.display_name,
              "default": m.default, "source": m.source} for m in mods],
            ensure_ascii=False, indent=2,
        ))
        return 0
    for m in mods:
        tags = " *DEFAULT*" if m.default else ""
        print(f"[{m.provider}] {m.id}{tags}  ({m.source})")
    return 0


def _cmd_chat(args: argparse.Namespace) -> int:
    provider, model = (None, args.model)
    if args.model:
        try:
            provider, model = route(args.model)
        except UnifiedError as e:
            print(f"모델 라우팅 실패: {e}", file=sys.stderr)
            return 2

    try:
        client = create(
            provider or "claude",
            model=model,
            web_search=args.web_search,
            cwd=args.cwd,
        )
    except UnifiedError as e:
        print(str(e), file=sys.stderr)
        return 3

    prompt = args.prompt or sys.stdin.read()

    try:
        if args.stream:
            for msg in client.stream(prompt):
                if msg.kind == "text" and msg.text:
                    print(msg.text, end="", flush=True)
                elif msg.kind == "tool_use":
                    name = (msg.tool or {}).get("name")
                    print(f"\n[tool_use: {name}]", flush=True)
            print()
        else:
            resp = client.chat(prompt)
            print(resp.text)
            print(
                f"\n---\nprovider={resp.provider}  model={resp.model}  "
                f"session_id={resp.session_id}  "
                f"in/out={resp.usage.input_tokens}/{resp.usage.output_tokens}",
                file=sys.stderr,
            )
    except UnifiedError as e:
        print(str(e), file=sys.stderr)
        return 4
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="unified-cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_doc = sub.add_parser("doctor", help="check binaries, auth, and models")
    p_doc.set_defaults(func=_cmd_doctor)

    p_mod = sub.add_parser("models", help="list available models")
    p_mod.add_argument("provider", nargs="?", choices=["claude", "codex", "gemini"])
    p_mod.add_argument("--refresh", action="store_true")
    p_mod.add_argument("--json", action="store_true")
    p_mod.set_defaults(func=_cmd_models)

    p_chat = sub.add_parser("chat", help="single-turn chat")
    p_chat.add_argument("prompt", nargs="?", help="prompt (or stdin)")
    p_chat.add_argument("-m", "--model", help="provider/model or model name")
    p_chat.add_argument("--stream", action="store_true")
    p_chat.add_argument("--no-web-search", dest="web_search",
                        action="store_false", default=True)
    p_chat.add_argument("--cwd")
    p_chat.set_defaults(func=_cmd_chat)

    ns = parser.parse_args(argv)
    return ns.func(ns)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
