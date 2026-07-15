"""Claude Code CLI provider."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Iterator, Optional

from ..base import BaseProvider
from ..core import Message, Response, Usage
from ..discovery import find_claude_bin
from ..errors import UnifiedError
from ..i18n import t


@dataclass
class _ClaudeStreamState:
    """Partial Claude content accumulated for one stream invocation only."""

    partial_text_by_block: dict[int, str] = field(default_factory=dict)
    partial_thinking_by_block: dict[int, str] = field(default_factory=dict)


class ClaudeProvider(BaseProvider):
    name = "claude"
    default_model = "claude-haiku-4-5"
    api_key_env = "ANTHROPIC_API_KEY"

    @classmethod
    def login_hint(cls) -> str:
        return t("err.claude.login_hint")

    # Legacy class attribute kept for backward compatibility (and an existing
    # test that inspects it). The value actually injected into the prompt is
    # resolved via i18n at USE time — see _terse_rule() — so the active language
    # wins; class-body strings evaluate before set_lang() can run.
    _TERSE_RULE = "답변은 질문이 요구하는 만큼만 간결하게 하세요. 추가 설명은 요청받을 때만 덧붙이세요."

    @classmethod
    def _terse_rule(cls) -> str:
        return t("err.claude.terse_rule")

    def __init__(
        self,
        *,
        system_prompt: Optional[str] = None,
        append_system_prompt: Optional[str] = None,
        allowed_tools: Optional[list[str]] = None,
        disallowed_tools: Optional[list[str]] = None,
        permission_mode: Optional[str] = None,
        add_dirs: Optional[list[str]] = None,
        safe_mode: bool = False,
        tools: Optional[list[str]] = None,
        restrict_image_reads: bool = False,
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
        self.safe_mode = safe_mode
        self.tools = None if tools is None else list(tools)
        self.restrict_image_reads = restrict_image_reads

        # This narrow profile is used by the local HTTP server. It must not be
        # combined with broad caller-provided capabilities: in dontAsk mode a
        # scoped Read rule is the only intended noninteractive file access.
        if self.restrict_image_reads:
            if not self.safe_mode:
                raise ValueError("restrict_image_reads requires safe_mode=True")
            if self.permission_mode != "dontAsk":
                raise ValueError(
                    "restrict_image_reads requires permission_mode='dontAsk'"
                )
            if self.web_search:
                raise ValueError("restrict_image_reads requires web_search=False")
            if self.allowed_tools:
                raise ValueError("restrict_image_reads does not accept allowed_tools")
            if self.add_dirs:
                raise ValueError("restrict_image_reads does not accept add_dirs")
            if self.tools not in (None, []):
                raise ValueError(
                    "restrict_image_reads only permits an empty tools list"
                )
            # Text-only server calls expose no Claude tools. Image calls change
            # this to ["Read"] only after materializing their exact files.
            self.tools = []

        if terse:
            terse_rule = self._terse_rule()
            self.append_system_prompt = (
                f"{self.append_system_prompt}\n\n{terse_rule}"
                if self.append_system_prompt else terse_rule
            )

        if self.web_search:
            for t in ("WebSearch", "WebFetch"):
                if t not in self.allowed_tools:
                    self.allowed_tools.append(t)

    @classmethod
    def _discover_bin(cls) -> Optional[str]:
        return find_claude_bin()

    @classmethod
    def _install_hint(cls) -> str:
        return t("err.claude.install_hint")

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
        args += ["--output-format", "stream-json", "--verbose", "--include-partial-messages"] if streaming \
            else ["--output-format", "json"]

        args += ["--model", model or self.model]
        if self.safe_mode:
            args += ["--safe-mode"]

        if self.system_prompt is not None:
            args += ["--system-prompt", self.system_prompt]
        if self.append_system_prompt is not None:
            args += ["--append-system-prompt", self.append_system_prompt]
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
        # Byte inputs are materialized to a temp file. URL inputs are rejected
        # rather than fetched implicitly; the model receives only an absolute
        # local path that the caller supplied or this wrapper created.
        allowed_tools = list(self.allowed_tools)
        effective_tools = self.tools
        if images:
            paths = [self._materialize_image(att) for att in self._normalize_images(images)]
            if self.restrict_image_reads:
                # Claude permission rules use // for an absolute filesystem
                # path. Do not pass broad Read here: it would allow a remote
                # prompt to exfiltrate arbitrary local files through the
                # server's response.
                allowed_tools = [self._read_rule(path) for path in paths]
                effective_tools = ["Read"]
            elif "Read" not in allowed_tools:
                allowed_tools.append("Read")
            path_lines = "\n".join(t("err.claude.image_label", path=p) for p in paths)
            prompt = (
                f"{path_lines}\n{t('err.claude.image_instruction')}\n{prompt}"
            )

        if effective_tools is not None:
            # An empty string is intentional: Claude's --tools "" disables
            # every built-in tool for a text-only restricted server call.
            args += ["--tools", ",".join(effective_tools)]
        if allowed_tools:
            args += ["--allowedTools", ",".join(allowed_tools)]

        # End option parsing so the positional prompt is never mis-parsed:
        # - a prompt starting with "-" (e.g. "--version") would be read as a flag;
        # - --tools/--allowedTools/--disallowedTools are VARIADIC (<tools...>) in
        #   the claude CLI, so when one of them is the last option it swallows the
        #   positional prompt ("Input must be provided either through stdin or as
        #   a prompt argument"). web_search=True hits this via --allowedTools.
        # Guarded so plain prompts without tool flags stay byte-identical.
        needs_sentinel = (
            prompt.startswith("-")
            or bool(allowed_tools)
            or effective_tools is not None
            or bool(self.disallowed_tools)
        )
        if needs_sentinel:
            args.append("--")
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
            self._register_temp_file(tmp)  # cleaned up after the call
            # macOS commonly exposes its temporary directory through /var
            # while the canonical target is /private/var. The restricted
            # server profile grants Claude a scoped Read rule for the target,
            # and Claude requires both a symlink path and target to match.
            # Use the canonical target in the prompt as well as the rule so a
            # valid byte image is not denied under dontAsk mode. Keep `tmp` in
            # the scope registration above: unlinking either spelling removes
            # the same file.
            return _os.path.realpath(_os.path.abspath(tmp))
        if att.url:
            raise UnifiedError(
                kind="config", provider="claude",
                message=t("err.claude.image_url_only"),
            )
        raise UnifiedError(
            kind="config", provider="claude",
            message=t("err.claude.empty_image"),
        )

    @staticmethod
    def _read_rule(path: str) -> str:
        """Return a Claude scoped-Read allow rule for one resolved file.

        Claude permission patterns use a double slash for filesystem-absolute
        paths. Resolving symlinks is essential: allow rules require both the
        link and target to match, and a temp-file profile must not accidentally
        approve a link whose target lies outside its intended file.
        """
        resolved = os.path.realpath(os.path.abspath(path)).replace("\\", "/")
        if len(resolved) >= 2 and resolved[1] == ":":
            # Claude normalizes Windows C:\\path to /c/path in permission
            # rules, so make the path form explicit and platform-independent.
            resolved = f"/{resolved[0].lower()}{resolved[2:]}"
        return f"Read(//{resolved.lstrip('/')})"

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

    def _new_stream_state(self) -> _ClaudeStreamState:
        return _ClaudeStreamState()

    @staticmethod
    def _seen_partial(partials: dict[int, str], index: int, text: str) -> str:
        """Return the already-streamed prefix of a complete content block.

        The claude CLI emits one assistant envelope per content block and
        re-indexes each envelope's content from 0, so the envelope index can
        disagree with the stream_event block index (e.g. text streamed as
        block 1 after a thinking block arrives in an envelope where it is
        block 0). Try the direct index first, then fall back to the
        concatenation of every streamed partial of this kind — without the
        fallback the complete text is yielded a second time (duplication).
        """
        seen = partials.get(index, "")
        if seen and text.startswith(seen):
            return seen
        joined = "".join(partials[k] for k in sorted(partials))
        if joined and text.startswith(joined):
            return joined
        return ""

    def _stream_normalize(
        self, obj: dict, state: object
    ) -> Iterator[Message]:
        # BaseProvider supplies a distinct state object to every sync/async
        # stream invocation. Be defensive for integrations calling this hook
        # directly, but never keep parser state on the provider instance.
        if not isinstance(state, _ClaudeStreamState):
            state = _ClaudeStreamState()
        yield from self._normalize_with_state(obj, state)

    def _normalize(self, obj: dict) -> Iterator[Message]:
        """Normalize one standalone event (private compatibility helper).

        Public streams call ``_stream_normalize`` with persistent per-stream
        state. A raw private call has no invocation context, so it is treated
        as a standalone event rather than leaking state into future calls.
        """
        yield from self._normalize_with_state(obj, _ClaudeStreamState())

    def _normalize_with_state(
        self, obj: dict, state: _ClaudeStreamState
    ) -> Iterator[Message]:
        t = obj.get("type", "")
        sid = obj.get("session_id")

        if t == "system":
            if sid:
                yield Message(kind="session", provider="claude", session_id=sid, raw=obj)
            return

        if t == "stream_event":
            event = obj.get("event") or {}
            if event.get("type") != "content_block_delta":
                return
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            index = event.get("index", 0)
            if not isinstance(index, int):
                index = 0
            if dtype == "text_delta":
                text = delta.get("text") or ""
                if text:
                    state.partial_text_by_block[index] = (
                        state.partial_text_by_block.get(index, "") + text
                    )
                    yield Message(kind="text", provider="claude", text=text, raw=obj)
            elif dtype == "thinking_delta":
                text = delta.get("thinking") or ""
                if text:
                    state.partial_thinking_by_block[index] = (
                        state.partial_thinking_by_block.get(index, "") + text
                    )
                    yield Message(kind="reasoning", provider="claude", text=text, raw=obj)
            return

        if t in ("assistant", "user"):
            inner = obj.get("message") or {}
            for index, block in enumerate(inner.get("content") or []):
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text") or ""
                    seen = self._seen_partial(state.partial_text_by_block, index, text)
                    if seen:
                        text = text[len(seen):]
                    if text:
                        yield Message(kind="text", provider="claude", text=text, raw=obj)
                elif btype == "thinking":
                    text = block.get("thinking") or ""
                    seen = self._seen_partial(state.partial_thinking_by_block, index, text)
                    if seen:
                        text = text[len(seen):]
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
            # Claude can emit another assistant envelope after a tool loop and
            # restart content-block indices at zero. The partial deltas above
            # belong only to this envelope, so retaining them would strip a
            # coincidentally matching prefix from a later complete message.
            state.partial_text_by_block.clear()
            state.partial_thinking_by_block.clear()
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
                message=t("err.claude.no_json"),
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
        # Prefer the requested model when Claude reports multiple modelUsage
        # entries. Claude Code can include a small haiku classifier/preamble entry
        # before the actual requested model (e.g. opus), so blindly taking the
        # first key misreports audits as haiku.
        model_usage = data.get("modelUsage") or {}
        if model in model_usage:
            resolved_model = model
        elif model == "opus" and "claude-opus-4-7" in model_usage:
            resolved_model = "claude-opus-4-7"
        elif model == "sonnet" and "claude-sonnet-4-6" in model_usage:
            resolved_model = "claude-sonnet-4-6"
        elif model == "haiku" and "claude-haiku-4-5-20251001" in model_usage:
            resolved_model = "claude-haiku-4-5-20251001"
        else:
            resolved_model = next(iter(model_usage), None) or model
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
