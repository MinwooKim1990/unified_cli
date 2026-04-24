"""Claude Code CLI provider."""

from __future__ import annotations

import json
from typing import Iterator, Optional

from ..base import BaseProvider
from ..core import Message, Response, Usage
from ..discovery import find_claude_bin
from ..errors import UnifiedError


class ClaudeProvider(BaseProvider):
    name = "claude"
    default_model = "claude-haiku-4-5"
    api_key_env = "ANTHROPIC_API_KEY"
    login_hint = "`claude /login` 을 재실행하세요."

    def __init__(
        self,
        *,
        system_prompt: Optional[str] = None,
        append_system_prompt: Optional[str] = None,
        allowed_tools: Optional[list[str]] = None,
        disallowed_tools: Optional[list[str]] = None,
        permission_mode: Optional[str] = None,
        add_dirs: Optional[list[str]] = None,
        **kw,
    ):
        super().__init__(**kw)
        self.system_prompt = system_prompt
        self.append_system_prompt = append_system_prompt
        self.allowed_tools = list(allowed_tools or [])
        self.disallowed_tools = list(disallowed_tools or [])
        self.permission_mode = permission_mode
        self.add_dirs = list(add_dirs or [])

        if self.web_search:
            for t in ("WebSearch", "WebFetch"):
                if t not in self.allowed_tools:
                    self.allowed_tools.append(t)
            if self.permission_mode is None:
                self.permission_mode = "bypassPermissions"

    @classmethod
    def _discover_bin(cls) -> Optional[str]:
        return find_claude_bin()

    @classmethod
    def _install_hint(cls) -> str:
        return ("Claude Desktop 앱 설치 또는 `npm i -g @anthropic-ai/claude-code`. "
                "또는 CLAUDE_CLI_PATH 환경변수로 경로 지정.")

    def _build_args(
        self,
        prompt: str,
        *,
        session_id: Optional[str],
        resume_last: bool,
        model: Optional[str],
        streaming: bool,
    ) -> list[str]:
        args: list[str] = [self.bin_path, "-p"]
        args += ["--output-format", "stream-json", "--verbose"] if streaming \
            else ["--output-format", "json"]

        args += ["--model", model or self.model]

        if self.system_prompt is not None:
            args += ["--system-prompt", self.system_prompt]
        if self.append_system_prompt is not None:
            args += ["--append-system-prompt", self.append_system_prompt]
        if self.allowed_tools:
            args += ["--allowedTools", ",".join(self.allowed_tools)]
        if self.disallowed_tools:
            args += ["--disallowedTools", ",".join(self.disallowed_tools)]
        if self.permission_mode:
            args += ["--permission-mode", self.permission_mode]
        for d in self.add_dirs:
            args += ["--add-dir", d]

        if session_id:
            args += ["--resume", session_id]
        elif resume_last:
            args += ["--continue"]

        args.append(prompt)
        return args

    def _normalize(self, obj: dict) -> Iterator[Message]:
        t = obj.get("type", "")
        sid = obj.get("session_id")

        if t == "system":
            if sid:
                yield Message(kind="session", provider="claude", session_id=sid, raw=obj)
            return

        if t in ("assistant", "user"):
            inner = obj.get("message") or {}
            for block in inner.get("content") or []:
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text") or ""
                    if text:
                        yield Message(kind="text", provider="claude", text=text, raw=obj)
                elif btype == "thinking":
                    text = block.get("thinking") or ""
                    if text:
                        yield Message(kind="reasoning", provider="claude", text=text, raw=obj)
                elif btype == "tool_use":
                    yield Message(
                        kind="tool_use", provider="claude",
                        tool={"name": block.get("name"), "input": block.get("input"),
                              "id": block.get("id")},
                        raw=obj,
                    )
                elif btype == "tool_result":
                    yield Message(
                        kind="tool_result", provider="claude",
                        tool={"id": block.get("tool_use_id"),
                              "output": block.get("content"),
                              "is_error": block.get("is_error", False)},
                        raw=obj,
                    )
            return

        if t == "result":
            usage = obj.get("usage") or {}
            if usage:
                yield Message(
                    kind="usage", provider="claude",
                    usage=Usage(
                        input_tokens=usage.get("input_tokens"),
                        output_tokens=usage.get("output_tokens"),
                        cached_tokens=usage.get("cache_read_input_tokens"),
                    ),
                    raw=obj,
                )
            result_text = obj.get("result") or ""
            if result_text:
                yield Message(kind="text", provider="claude", text=result_text, raw=obj)
            if sid:
                yield Message(kind="session", provider="claude", session_id=sid, raw=obj)
            yield Message(kind="done", provider="claude", raw=obj)
            return

        if t == "error":
            yield Message(
                kind="error", provider="claude",
                error=str(obj.get("error") or obj), raw=obj,
            )

    def _parse_json_response(self, text: str, model: str) -> Response:
        start = text.find("{")
        if start < 0:
            raise UnifiedError(
                kind="internal", provider="claude",
                message="Claude CLI 응답에 JSON이 없습니다.",
                cause=text[:300],
            )
        data = json.loads(text[start:])
        usage_raw = data.get("usage") or {}
        return Response(
            text=data.get("result", ""),
            session_id=data.get("session_id", ""),
            provider="claude",
            model=model,
            usage=Usage(
                input_tokens=usage_raw.get("input_tokens"),
                output_tokens=usage_raw.get("output_tokens"),
                cached_tokens=usage_raw.get("cache_read_input_tokens"),
            ),
            messages=[],
            raw=[data],
        )
