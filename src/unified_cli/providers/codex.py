"""OpenAI Codex CLI provider."""

from __future__ import annotations

import json
import math
import os
import re
from typing import Any, Iterator, Optional

from ..base import BaseProvider
from ..core import Message, Response, Usage
from ..discovery import find_codex_bin
from ..errors import UnifiedError
from ..i18n import t


class CodexProvider(BaseProvider):
    name = "codex"
    default_model = "gpt-5.4-mini"
    api_key_env = "OPENAI_API_KEY"

    _CONFIG_KEY_RE = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\Z")

    @classmethod
    def login_hint(cls) -> str:
        return t("err.codex.login_hint")

    def __init__(
        self,
        *,
        sandbox: str = "read-only",
        full_auto: bool = False,
        dangerously_bypass: bool = False,
        skip_git_check: bool = True,
        ephemeral: bool = False,
        ignore_user_config: bool = False,
        ignore_rules: bool = False,
        add_dirs: Optional[list[str]] = None,
        config_overrides: Optional[dict] = None,
        **kw,
    ):
        super().__init__(**kw)
        self.sandbox = sandbox
        self.full_auto = full_auto
        self.dangerously_bypass = dangerously_bypass
        self.skip_git_check = skip_git_check
        self.ephemeral = ephemeral
        self.ignore_user_config = ignore_user_config
        self.ignore_rules = ignore_rules
        self.add_dirs = list(add_dirs or [])
        self.config_overrides = dict(config_overrides or {})

        # Web search: must use -c tools.web_search=true (not --search, which
        # is only on top-level `codex`, not `codex exec`).
        if self.web_search:
            self.config_overrides.setdefault("tools.web_search", True)
        # Fail fast on malformed configuration rather than passing a value that
        # the CLI may parse differently across releases.
        self._config_args()

    @classmethod
    def _discover_bin(cls) -> Optional[str]:
        return find_codex_bin()

    @classmethod
    def _install_hint(cls) -> str:
        return t("err.codex.install_hint")

    def _common_flags(self, model: Optional[str], streaming: bool) -> list[str]:
        args = ["--json"] if streaming else ["--json"]
        # Codex only supports --json for both streaming and non-streaming; we
        # aggregate events ourselves for non-streaming.
        if self.skip_git_check:
            args += ["--skip-git-repo-check"]
        if self.ephemeral:
            args += ["--ephemeral"]
        if self.ignore_user_config:
            args += ["--ignore-user-config"]
        if self.ignore_rules:
            args += ["--ignore-rules"]
        args += ["-m", model or self.model]
        if self.full_auto:
            args += ["--full-auto"]
        elif self.dangerously_bypass:
            args += ["--dangerously-bypass-approvals-and-sandbox"]
        else:
            if self.sandbox:
                args += ["-s", self.sandbox]
        args += self._config_args()
        return args

    @classmethod
    def _toml_literal(cls, value: Any) -> str:
        """Serialize an accepted Python value as one TOML literal.

        Codex parses the value after ``-c key=`` as TOML. Quoting strings here
        avoids accidental type changes or line/config injection, while native
        booleans and numbers retain their intended TOML types.
        """
        if isinstance(value, str):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("Codex config override floats must be finite")
            return repr(value)
        if isinstance(value, (list, tuple)):
            return "[" + ", ".join(cls._toml_literal(item) for item in value) + "]"
        raise ValueError(
            "Codex config override values must be str, bool, int, finite float, or list"
        )

    def _config_args(self) -> list[str]:
        """Return validated ``-c key=value`` pairs shared by exec and resume."""
        args: list[str] = []
        for key, value in self.config_overrides.items():
            if not isinstance(key, str) or not self._CONFIG_KEY_RE.fullmatch(key):
                raise ValueError(
                    "Codex config override keys must be dotted bare TOML keys"
                )
            args += ["-c", f"{key}={self._toml_literal(value)}"]
        return args

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
        argv: list[str] = [self.bin_path, "exec"]

        # Codex `exec` natively supports `-i <FILE>` (repeatable) for inline
        # image attachments. NOTE: as of codex CLI 0.129, when `-i` is used
        # the prompt MUST be passed via stdin (argv prompt is ignored and
        # the CLI complains "No prompt provided via stdin"). We therefore
        # send prompt via stdin whenever images are attached, and via argv
        # otherwise.
        image_args = self._image_args(images)
        use_stdin = bool(image_args)

        if session_id or resume_last:
            # `resume` subcommand: no -s / --add-dir / -C
            argv += ["resume", "--json"]
            if self.skip_git_check:
                argv += ["--skip-git-repo-check"]
            if self.ephemeral:
                argv += ["--ephemeral"]
            if self.ignore_user_config:
                argv += ["--ignore-user-config"]
            if self.ignore_rules:
                argv += ["--ignore-rules"]
            argv += ["-m", model or self.model]
            if self.dangerously_bypass:
                argv += ["--dangerously-bypass-approvals-and-sandbox"]
            argv += self._config_args()
            argv += image_args
            if resume_last:
                argv += ["--last"]
            else:
                argv += [session_id]  # type: ignore[list-item]
            if not use_stdin:
                if prompt.startswith("-"):
                    argv += ["--"]  # a "--flag"-like prompt is text, not options
                argv += [prompt]
            return argv, (prompt if use_stdin else None)

        argv += self._common_flags(model, streaming)
        if self.cwd:
            argv += ["-C", self.cwd]
        for d in self.add_dirs:
            argv += ["--add-dir", d]
        argv += image_args
        if not use_stdin:
            if prompt.startswith("-"):
                argv += ["--"]  # a "--flag"-like prompt is text, not options
            argv += [prompt]
        return argv, (prompt if use_stdin else None)

    def _image_args(self, images) -> list[str]:
        """Translate normalized images into `-i <path>` repeated args.

        Bytes / URL inputs are written to a temp file so the CLI can read them
        as paths. Temp files are registered for cleanup after the call (see
        BaseProvider._register_temp_file / _cleanup_temp_files).
        """
        if not images:
            return []
        from ..core import normalize_images
        out: list[str] = []
        for att in normalize_images(images):
            path = self._materialize(att)
            out += ["-i", path]
        return out

    def _materialize(self, att) -> str:
        """Ensure attachment is on disk; return path."""
        if att.path:
            return att.path
        if att.bytes_:
            import tempfile
            ext = (att.media_type or "image/png").split("/")[-1]
            ext = "jpg" if ext == "jpeg" else ext
            fd, tmp = tempfile.mkstemp(prefix="unified_cli_img_", suffix=f".{ext}")
            with os.fdopen(fd, "wb") as f:
                f.write(att.bytes_)
            self._register_temp_file(tmp)  # cleaned up after the call
            return tmp
        if att.url:
            # Codex CLI wants a local path; a URL would need download.
            # Defer that to the caller — explicit error here.
            raise UnifiedError(
                kind="config", provider="codex",
                message=t("err.codex.image_url_only"),
            )
        raise UnifiedError(
            kind="config", provider="codex",
            message=t("err.codex.empty_image"),
        )

    def _normalize(self, obj: dict) -> Iterator[Message]:
        t = obj.get("type", "")

        if t == "thread.started":
            tid = obj.get("thread_id")
            if tid:
                yield Message(kind="session", provider="codex", session_id=tid, raw=obj)
            return

        if t == "item.completed":
            item = obj.get("item") or {}
            itype = item.get("type", "")
            if itype == "agent_message":
                text = item.get("text") or ""
                if text:
                    yield Message(kind="text", provider="codex", text=text, raw=obj)
            elif itype == "reasoning":
                text = item.get("text") or ""
                if text:
                    yield Message(kind="reasoning", provider="codex", text=text, raw=obj)
            elif itype == "web_search":
                action = item.get("action") or {}
                yield Message(
                    kind="tool_use", provider="codex",
                    tool={"name": "web_search", "input": action, "id": item.get("id")},
                    raw=obj,
                )
            elif itype in ("function_call", "tool_call"):
                yield Message(
                    kind="tool_use", provider="codex",
                    tool={"name": item.get("name"), "input": item.get("arguments"),
                          "id": item.get("id")},
                    raw=obj,
                )
            elif itype in ("function_call_output", "tool_output", "command_execution"):
                yield Message(
                    kind="tool_result", provider="codex",
                    tool={"id": item.get("id"), "output": item.get("output") or item,
                          "is_error": bool(item.get("error"))},
                    raw=obj,
                )
            return

        if t == "turn.completed":
            usage = obj.get("usage") or {}
            yield Message(
                kind="usage", provider="codex",
                usage=Usage(
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    cached_tokens=usage.get("cached_input_tokens"),
                ),
                raw=obj,
            )
            yield Message(kind="done", provider="codex", raw=obj)
            return

        if t in ("error", "turn.failed"):
            err = obj.get("error") or obj.get("message") or obj
            yield Message(kind="error", provider="codex",
                          error=str(err), raw=obj)

    def _parse_json_response(self, text: str, model: str) -> Response:
        """Codex `--json` emits NDJSON even for non-streaming; aggregate."""
        events: list[dict] = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                import json as _json
                events.append(_json.loads(line))
            except Exception:
                continue

        texts: list[str] = []
        session_id = ""
        usage = Usage()
        has_error: Optional[str] = None

        for ev in events:
            for msg in self._normalize(ev):
                if msg.kind == "text" and msg.text:
                    texts.append(msg.text)
                elif msg.kind == "session" and msg.session_id:
                    session_id = msg.session_id
                elif msg.kind == "usage" and msg.usage:
                    usage = msg.usage
                elif msg.kind == "error":
                    has_error = msg.error

        if has_error and not texts:
            raise UnifiedError(
                kind="internal", provider="codex",
                message=t("err.codex.turn_error"),
                cause=str(has_error)[:300],
            )

        return Response(
            text="\n".join(texts),
            session_id=session_id,
            provider="codex",
            model=model,
            usage=usage,
            messages=[],
            raw=events,
        )
