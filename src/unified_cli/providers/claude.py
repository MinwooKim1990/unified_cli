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

    _TERSE_RULE = "답변은 질문이 요구하는 만큼만 간결하게 하세요. 추가 설명은 요청받을 때만 덧붙이세요."

    def __init__(
        self,
        *,
        system_prompt: Optional[str] = None,
        append_system_prompt: Optional[str] = None,
        allowed_tools: Optional[list[str]] = None,
        disallowed_tools: Optional[list[str]] = None,
        permission_mode: Optional[str] = None,
        add_dirs: Optional[list[str]] = None,
        terse: bool = False,
        **kw,
    ):
        super().__init__(**kw)
        self.system_prompt = system_prompt
        self.append_system_prompt = append_system_prompt
        self.allowed_tools = list(allowed_tools or [])
        self.disallowed_tools = list(disallowed_tools or [])
        self.permission_mode = permission_mode
        self.add_dirs = list(add_dirs or [])

        if terse:
            self.append_system_prompt = (
                f"{self.append_system_prompt}\n\n{self._TERSE_RULE}"
                if self.append_system_prompt else self._TERSE_RULE
            )

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
        images: Optional[list] = None,
    ) -> tuple[list[str], Optional[str]]:
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

        # Image input via the Read tool. Claude Code's built-in `Read` tool
        # natively vision-processes PNG/JPEG/GIF/WebP files. We don't pipe
        # base64 (which the headless mode silently drops); instead we ensure
        # the file is on disk somewhere accessible, allow the Read tool, and
        # prepend the path to the prompt so the model picks it up.
        #
        # Bytes/URL inputs are materialized to a temp file under cwd so that
        # `--add-dir` (already in args via self.add_dirs) doesn't need to be
        # extended dynamically — the model only needs Read access to the
        # absolute path.
        if images:
            allowed_for_image = list(self.allowed_tools)
            if "Read" not in allowed_for_image:
                allowed_for_image.append("Read")
            # Replace any earlier --allowedTools we already added with the
            # extended list (last write wins).
            args = self._strip_existing_allowed_tools(args)
            args += ["--allowedTools", ",".join(allowed_for_image)]
            # Bypass permissions for unattended image processing.
            args = self._strip_existing_permission_mode(args)
            args += ["--permission-mode", "bypassPermissions"]

            paths = [self._materialize_image(att) for att in self._normalize_images(images)]
            path_lines = "\n".join(f"이미지 파일: {p}" for p in paths)
            prompt = (
                f"{path_lines}\n위 이미지를 Read 도구로 읽고 다음 질문에 답해주세요:\n{prompt}"
            )

        args.append(prompt)
        return args, None

    @staticmethod
    def _normalize_images(images):
        from ..core import normalize_images
        return normalize_images(images)

    def _materialize_image(self, att) -> str:
        """Return absolute path to image — write bytes to temp file if needed."""
        from pathlib import Path as _Path
        if att.path:
            return str(_Path(att.path).resolve())
        if att.bytes_:
            import tempfile, os as _os
            ext = (att.media_type or "image/png").split("/")[-1]
            ext = "jpg" if ext == "jpeg" else ext
            fd, tmp = tempfile.mkstemp(prefix="unified_cli_img_", suffix=f".{ext}")
            with _os.fdopen(fd, "wb") as f:
                f.write(att.bytes_)
            return tmp
        if att.url:
            raise UnifiedError(
                kind="config", provider="claude",
                message="Claude Read 도구는 로컬 파일만 받습니다. URL 은 미리 다운로드하세요.",
            )
        raise UnifiedError(
            kind="config", provider="claude",
            message="비어있는 이미지 첨부.",
        )

    @staticmethod
    def _strip_existing_allowed_tools(args: list[str]) -> list[str]:
        out: list[str] = []
        i = 0
        while i < len(args):
            if args[i] == "--allowedTools" and i + 1 < len(args):
                i += 2
            else:
                out.append(args[i])
                i += 1
        return out

    @staticmethod
    def _strip_existing_permission_mode(args: list[str]) -> list[str]:
        out: list[str] = []
        i = 0
        while i < len(args):
            if args[i] == "--permission-mode" and i + 1 < len(args):
                i += 2
            else:
                out.append(args[i])
                i += 1
        return out

    @staticmethod
    def _aggregate_stream_json(body: str) -> dict:
        """Collect a Claude stream-json NDJSON output into a single dict
        compatible with the `--output-format json` shape (`result`, `usage`,
        `session_id`, `is_error`, `modelUsage`).
        """
        result_text_parts: list[str] = []
        session_id = ""
        usage: dict = {}
        model_usage: dict = {}
        is_error = False

        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = obj.get("type")
            if t == "system" and obj.get("session_id"):
                session_id = obj["session_id"]
            elif t == "assistant":
                msg = obj.get("message") or {}
                for block in msg.get("content") or []:
                    if block.get("type") == "text":
                        result_text_parts.append(block.get("text") or "")
            elif t == "result":
                if obj.get("is_error"):
                    is_error = True
                if obj.get("result"):
                    # `result.result` carries the final concatenated text.
                    # Prefer it over piecewise assistant chunks if present.
                    result_text_parts = [obj["result"]]
                if obj.get("usage"):
                    usage = obj["usage"]
                if obj.get("modelUsage"):
                    model_usage = obj["modelUsage"]
                if obj.get("session_id"):
                    session_id = obj["session_id"]

        return {
            "result": "".join(result_text_parts),
            "session_id": session_id,
            "usage": usage,
            "modelUsage": model_usage,
            "is_error": is_error,
        }

    @staticmethod
    def _upgrade_output_to_stream_json(args: list[str]) -> list[str]:
        """Replace `--output-format json` with `stream-json --verbose` if present."""
        out: list[str] = []
        i = 0
        while i < len(args):
            if args[i] == "--output-format" and i + 1 < len(args) and args[i + 1] == "json":
                out += ["--output-format", "stream-json", "--verbose"]
                i += 2
            else:
                out.append(args[i])
                i += 1
        return out

    def _build_stream_json_input(self, prompt: str, images: list) -> str:
        """Build an Anthropic Messages-style stream-json envelope for stdin.

        Format expected by `claude -p --input-format stream-json` (one JSON
        object per line, terminated by EOF / pipe close):

            {"type":"user","message":{"role":"user","content":[
                {"type":"image","source":{"type":"base64",
                    "media_type":"image/png","data":"..."}},
                {"type":"text","text":"prompt"}
            ]}}
        """
        from ..core import normalize_images, attachment_b64
        content = []
        for att in normalize_images(images):
            if att.url:
                content.append({
                    "type": "image",
                    "source": {"type": "url", "url": att.url},
                })
            else:
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": att.media_type or "image/png",
                        "data": attachment_b64(att),
                    },
                })
        content.append({"type": "text", "text": prompt})

        envelope = {
            "type": "user",
            "message": {"role": "user", "content": content},
        }
        return json.dumps(envelope, ensure_ascii=False) + "\n"

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
            # Note: Claude's stream-json emits the final text as an `assistant`
            # event before emitting `result`. `result.result` contains the same
            # text, so yielding it again would double-print during streaming.
            # We deliberately skip it here — the text was already streamed.
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

        # Two output forms:
        #   1. `--output-format json` → single JSON object
        #   2. `--output-format stream-json` → NDJSON, one event per line.
        #      Used when image input is attached (forced by --input-format
        #      stream-json). We aggregate the `result` event's data.
        body = text[start:].strip()
        if "\n{" in body:  # NDJSON
            data = self._aggregate_stream_json(body)
        else:
            data = json.loads(body)

        # Claude CLI returns 200 + a normal-looking JSON envelope even for
        # provider-side errors (bad model name, content policy, etc); the only
        # signal is `is_error: true`. Without this guard the error message
        # silently masquerades as a successful response.
        if data.get("is_error"):
            raise UnifiedError(
                kind="internal", provider="claude",
                message=str(data.get("result") or "Claude returned is_error"),
                cause=text[:300],
            )

        usage_raw = data.get("usage") or {}
        # Prefer the model the CLI actually resolved to (e.g. when alias `opus`
        # was passed, the resolved id is `claude-opus-4-7`). Surfaces silent
        # fallback to the user via Response.model.
        resolved_model = (
            (data.get("modelUsage") and next(iter(data["modelUsage"]), None))
            or model
        )
        return Response(
            text=data.get("result", ""),
            session_id=data.get("session_id", ""),
            provider="claude",
            model=resolved_model,
            usage=Usage(
                input_tokens=usage_raw.get("input_tokens"),
                output_tokens=usage_raw.get("output_tokens"),
                cached_tokens=usage_raw.get("cache_read_input_tokens"),
            ),
            messages=[],
            raw=[data],
        )
