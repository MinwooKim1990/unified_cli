#!/usr/bin/env python3
"""Offline fake for the bounded Grok Build adapter contract."""

import json
import os
import sys
import time


def emit(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def sidecar(suffix, default):
    path = os.path.realpath(sys.argv[0]) + suffix
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except FileNotFoundError:
        return default


args = sys.argv[1:]
fixed_environment = {
    "GROK_DISABLE_AUTOUPDATER": "1",
    "GROK_WRITE_FILE": "0",
    "GROK_TOOL_SEARCH": "0",
    "GROK_LSP_TOOLS": "0",
    "GROK_MEMORY": "0",
    "GROK_SUBAGENTS": "0",
    "GROK_WEB_FETCH": "0",
    "GROK_RESPECT_GITIGNORE": "1",
    "GROK_CURSOR_SKILLS_ENABLED": "false",
    "GROK_CURSOR_RULES_ENABLED": "false",
    "GROK_CURSOR_AGENTS_ENABLED": "false",
    "GROK_CURSOR_MCPS_ENABLED": "false",
    "GROK_CURSOR_HOOKS_ENABLED": "false",
    "GROK_CURSOR_SESSIONS_ENABLED": "false",
    "GROK_CLAUDE_SKILLS_ENABLED": "false",
    "GROK_CLAUDE_RULES_ENABLED": "false",
    "GROK_CLAUDE_AGENTS_ENABLED": "false",
    "GROK_CLAUDE_MCPS_ENABLED": "false",
    "GROK_CLAUDE_HOOKS_ENABLED": "false",
    "GROK_CLAUDE_SESSIONS_ENABLED": "false",
    "GROK_CODEX_SKILLS_ENABLED": "false",
    "GROK_CODEX_RULES_ENABLED": "false",
    "GROK_CODEX_AGENTS_ENABLED": "false",
    "GROK_CODEX_MCPS_ENABLED": "false",
    "GROK_CODEX_HOOKS_ENABLED": "false",
    "GROK_CODEX_SESSIONS_ENABLED": "false",
    "GROK_OFFICIAL_MARKETPLACE_AUTO_REGISTER": "0",
    "GROK_MARKETPLACE_REQUIRE_SHA": "1",
    "GROK_MANAGED_MCPS_ENABLED": "false",
    "GROK_MANAGED_MCP_GATEWAY_TOOLS_ENABLED": "false",
}
if any(os.environ.get(name) != value for name, value in fixed_environment.items()):
    raise SystemExit(95)
if args == ["--version"]:
    print(sidecar(".version", "grok 0.2.111 (94172f2aa4e5) [stable]"))
    raise SystemExit(0)

if args == ["--help"]:
    if sidecar(".identity", "official") != "official":
        print("Usage: grok-cli [OPTIONS]")
        print("  --prompt <PROMPT>")
    else:
        print("Grok Build TUI")
        print("Usage: grok [OPTIONS] [PROMPT] [COMMAND]")
        print("  -p, --single <PROMPT>        Single-turn prompt")
        print("  -r, --resume [<SESSION_ID>]  Resume a session")
        print("      --output-format <FORMAT> Output format")
    raise SystemExit(0)

if args == ["inspect", "--json"]:
    print('{"status":"ok"}')
    raise SystemExit(0)

fixed = [
    "--no-auto-update",
    "--sandbox",
    "strict",
    "--permission-mode",
    "dontAsk",
    "--tools",
    "read_file,grep,list_dir",
    "--allow",
    "Read",
    "--allow",
    "Grep",
    "--deny",
    "Bash",
    "--deny",
    "Edit",
    "--deny",
    "MCPTool",
    "--deny",
    "WebFetch",
    "--deny",
    "WebSearch",
    "--no-plan",
    "--no-subagents",
    "--no-memory",
    "--disable-web-search",
    "--output-format",
    "streaming-json",
]
if args[: len(fixed)] != fixed:
    raise SystemExit(91)
args = args[len(fixed) :]
if len(args) < 4 or args[0] != "-m":
    raise SystemExit(92)
model = args[1]
args = args[2:]
session = "session-new"
if args[:1] == ["-r"]:
    if len(args) < 4:
        raise SystemExit(93)
    session = args[1]
    args = args[2:]
if len(args) != 2 or args[0] != "-p":
    raise SystemExit(94)
prompt = args[1]
with open(os.path.realpath(sys.argv[0]) + ".prompt", "a", encoding="utf-8") as handle:
    handle.write(prompt + "\n")

if prompt == "nonzero":
    sys.stderr.write("provider failed\n")
    raise SystemExit(7)
if prompt == "cancel":
    while True:
        time.sleep(0.05)
if prompt == "flood":
    for index in range(100):
        emit({"type": "text", "data": str(index)})
    emit(
        {
            "type": "end",
            "stopReason": "complete",
            "sessionId": session,
            "requestId": "request-flood",
        }
    )
    raise SystemExit(0)
if prompt == "malformed":
    emit({"type": "text", "data": 7})
    raise SystemExit(0)
if prompt == "unknown":
    emit({"type": "mystery", "data": "secret"})
    raise SystemExit(0)
if prompt == "missing-end":
    emit({"type": "text", "data": "unfinished"})
    raise SystemExit(0)
if prompt in ("duplicate-end", "after-end"):
    end = {
        "type": "end",
        "stopReason": "complete",
        "sessionId": session,
        "requestId": "request-end",
    }
    emit(end)
    emit(end if prompt == "duplicate-end" else {"type": "text", "data": "late"})
    raise SystemExit(0)

emit({"type": "thought", "data": "never expose this"})
emit({"type": "text", "data": prompt})
end = {
    "type": "end",
    "stopReason": "complete",
    "sessionId": session,
    "requestId": "request-{}".format(model),
    "usage": {
        "input_tokens": 3,
        "cache_read_input_tokens": 1,
        "output_tokens": 2,
        "reasoning_tokens": 1,
        "total_tokens": 5,
    },
}
if prompt == "malformed-usage":
    end["usage"]["input_tokens"] = "3"
if prompt == "incomplete-usage":
    end.pop("usage")
    end["usage_is_incomplete"] = True
emit(end)
