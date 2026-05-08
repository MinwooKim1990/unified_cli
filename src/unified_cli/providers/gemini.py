"""Google Gemini CLI provider."""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Iterator, Optional

from ..base import BaseProvider
from ..core import Message, Response, Usage
from ..discovery import find_gemini_bin
from ..errors import UnifiedError, classify


_SESSION_LINE = re.compile(
    r"^\s*(\d+)\.\s+(.*?)\s+\([^)]*\)\s+\[([0-9a-fA-F\-]+)\]\s*$"
)


class GeminiProvider(BaseProvider):
    name = "gemini"
    default_model = "gemini-3.1-flash-lite-preview"
    api_key_env = "GEMINI_API_KEY"
    login_hint = "`gemini /auth` 를 재실행하세요."

    def __init__(
        self,
        *,
        approval_mode: Optional[str] = None,   # default | auto_edit | yolo | plan
        yolo: bool = False,
        sandbox: bool = False,
        skip_trust: bool = True,
        include_directories: Optional[list[str]] = None,
        extensions: Optional[list[str]] = None,
        allowed_mcp_servers: Optional[list[str]] = None,
        **kw,
    ):
        super().__init__(**kw)
        self.approval_mode = "yolo" if yolo else approval_mode
        self.sandbox = sandbox
        self.skip_trust = skip_trust
        self.include_directories = list(include_directories or [])
        self.extensions = extensions
        self.allowed_mcp_servers = allowed_mcp_servers
        # Gemini's google_web_search is ON by default. `web_search=False` is
        # approximated by switching approval_mode to "plan" (read-only, no
        # tool calls), unless user explicitly set another approval_mode.
        if not self.web_search and self.approval_mode is None:
            self.approval_mode = "plan"

    @classmethod
    def _discover_bin(cls) -> Optional[str]:
        return find_gemini_bin()

    @classmethod
    def _install_hint(cls) -> str:
        return "`npm i -g @google/gemini-cli`."

    def _env(self, fallback_api_key: bool = False) -> dict:
        env = super()._env(fallback_api_key)
        if self.skip_trust:
            env.setdefault("GEMINI_CLI_TRUST_WORKSPACE", "true")
        return env

    def _common_flags(
        self, model: Optional[str], streaming: bool,
        *, allow_tools: bool = False,
    ) -> list[str]:
        args: list[str] = [
            "--output-format", "stream-json" if streaming else "json",
        ]
        if self.skip_trust:
            args += ["--skip-trust"]
        args += ["-m", model or self.model]
        # `--approval-mode plan` is read-only and blocks tool calls — but
        # Gemini treats `@<path>` image refs as a tool invocation, so plan
        # mode silently swallows them. When the caller wires up images we
        # bypass plan and rely on the default approval policy instead.
        approval = None if allow_tools and self.approval_mode == "plan" \
                        else self.approval_mode
        if approval:
            args += ["--approval-mode", approval]
        if self.sandbox:
            args += ["-s"]
        if self.include_directories:
            args += ["--include-directories", ",".join(self.include_directories)]
        if self.extensions is not None:
            for e in self.extensions:
                args += ["-e", e]
        if self.allowed_mcp_servers is not None:
            args += ["--allowed-mcp-server-names", ",".join(self.allowed_mcp_servers)]
        return args

    def _find_session_index(self, session_id: str) -> int:
        """Parse --list-sessions output to find the index for a given UUID."""
        args = [self.bin_path, "--list-sessions"]
        if self.skip_trust:
            args += ["--skip-trust"]
        out = subprocess.run(
            args, capture_output=True, text=True,
            cwd=self.cwd, env=self._env(), timeout=self.timeout,
        )
        if out.returncode != 0:
            raise classify(self.name, out.stderr, out.stdout, out.returncode)
        for line in out.stdout.splitlines():
            m = _SESSION_LINE.match(line)
            if m and m.group(3).lower() == session_id.lower():
                return int(m.group(1))
        raise UnifiedError(
            kind="not_found", provider="gemini",
            message=f"session_id {session_id} 를 현재 프로젝트에서 찾을 수 없습니다.",
            hint="같은 cwd 에서 실행 중인지 확인하세요.",
        )

    def _build_args(
        self,
        prompt: str,
        *,
        session_id: Optional[str],
        resume_last: bool,
        model: Optional[str],
        streaming: bool,
        images: Optional[list] = None,
    ) -> tuple[list[str], Optional[str]]:
        args = [self.bin_path] + self._common_flags(
            model, streaming, allow_tools=bool(images),
        )
        if session_id:
            idx = self._find_session_index(session_id)
            args += ["-r", str(idx)]
        elif resume_last:
            args += ["-r", "latest"]

        # Gemini CLI has no dedicated image flag in `-p` headless mode. Its
        # interactive parser recognizes `@path` references inside the prompt
        # and inlines the file content. We splice those in front of the user
        # prompt and rely on this same syntax surviving headless mode (which
        # current Gemini CLI versions appear to). Bytes/URL inputs are
        # materialized to a temp file first.
        full_prompt = self._inject_image_refs(prompt, images)
        args += ["-p", full_prompt]
        return args, None

    def _inject_image_refs(self, prompt: str, images) -> str:
        if not images:
            return prompt
        from ..core import normalize_images
        refs = []
        for att in normalize_images(images):
            path = self._materialize(att)
            refs.append(f"@{path}")
        return " ".join(refs) + " " + prompt

    def _materialize(self, att) -> str:
        if att.path:
            return att.path
        if att.bytes_:
            import tempfile
            ext = (att.media_type or "image/png").split("/")[-1]
            ext = "jpg" if ext == "jpeg" else ext
            fd, tmp = tempfile.mkstemp(prefix="unified_cli_img_", suffix=f".{ext}")
            with os.fdopen(fd, "wb") as f:
                f.write(att.bytes_)
            return tmp
        if att.url:
            raise UnifiedError(
                kind="config", provider="gemini",
                message="Gemini @<path> 는 로컬 파일만 받습니다. URL 은 미리 다운로드하세요.",
            )
        raise UnifiedError(
            kind="config", provider="gemini",
            message="비어있는 이미지 첨부.",
        )

    def _normalize(self, obj: dict) -> Iterator[Message]:
        t = obj.get("type", "")

        if t == "init":
            sid = obj.get("session_id")
            if sid:
                yield Message(kind="session", provider="gemini",
                              session_id=sid, raw=obj)
            return

        if t == "message":
            role = obj.get("role")
            content = obj.get("content")
            if role == "assistant" and content:
                yield Message(kind="text", provider="gemini",
                              text=content, raw=obj)
            return

        if t == "tool_use":
            yield Message(
                kind="tool_use", provider="gemini",
                tool={"name": obj.get("tool_name"),
                      "input": obj.get("parameters"),
                      "id": obj.get("tool_id")},
                raw=obj,
            )
            return

        if t == "tool_result":
            yield Message(
                kind="tool_result", provider="gemini",
                tool={"id": obj.get("tool_id"),
                      "output": obj.get("output"),
                      "is_error": obj.get("status") != "success"},
                raw=obj,
            )
            return

        if t == "result":
            stats = obj.get("stats") or {}
            yield Message(
                kind="usage", provider="gemini",
                usage=Usage(
                    input_tokens=stats.get("input_tokens"),
                    output_tokens=stats.get("output_tokens"),
                    cached_tokens=stats.get("cached"),
                    total_tokens=stats.get("total_tokens"),
                ),
                raw=obj,
            )
            yield Message(kind="done", provider="gemini", raw=obj)
            return

        if t == "error":
            err = obj.get("error") or obj.get("message") or obj
            yield Message(kind="error", provider="gemini",
                          error=str(err), raw=obj)

    def _parse_json_response(self, text: str, model: str) -> Response:
        start = text.find("{")
        if start < 0:
            raise UnifiedError(
                kind="internal", provider="gemini",
                message="Gemini CLI 응답에 JSON이 없습니다.",
                cause=text[:300],
            )
        data = json.loads(text[start:])

        # Aggregate router+main model token stats.
        tot_in = tot_out = tot_cached = tot = 0
        models_stats = (data.get("stats") or {}).get("models") or {}
        for m in models_stats.values():
            tks = (m.get("tokens") or {})
            tot_in += tks.get("input", 0) or tks.get("prompt", 0)
            tot_out += tks.get("candidates", 0) or tks.get("output", 0)
            tot_cached += tks.get("cached", 0)
            tot += tks.get("total", 0)

        if data.get("error"):
            raise UnifiedError(
                kind="internal", provider="gemini",
                message=str(data["error"].get("message")
                            if isinstance(data["error"], dict)
                            else data["error"]),
                cause=text[:300],
            )

        return Response(
            text=data.get("response", ""),
            session_id=data.get("session_id", ""),
            provider="gemini",
            model=model,
            usage=Usage(
                input_tokens=tot_in or None,
                output_tokens=tot_out or None,
                cached_tokens=tot_cached or None,
                total_tokens=tot or None,
            ),
            messages=[],
            raw=[data],
        )

    def list_sessions(self) -> list[dict]:
        args = [self.bin_path, "--list-sessions"]
        if self.skip_trust:
            args += ["--skip-trust"]
        out = subprocess.run(
            args, capture_output=True, text=True,
            cwd=self.cwd, env=self._env(), timeout=self.timeout,
        )
        sessions: list[dict] = []
        for line in out.stdout.splitlines():
            m = _SESSION_LINE.match(line)
            if m:
                sessions.append({
                    "index": int(m.group(1)),
                    "title": m.group(2),
                    "session_id": m.group(3),
                })
        return sessions
