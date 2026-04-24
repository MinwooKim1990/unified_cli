"""OpenAI Codex CLI provider."""

from __future__ import annotations

from typing import Iterator, Optional

from ..base import BaseProvider
from ..core import Message, Response, Usage
from ..discovery import find_codex_bin
from ..errors import UnifiedError


class CodexProvider(BaseProvider):
    name = "codex"
    default_model = "gpt-5.4-mini"
    api_key_env = "OPENAI_API_KEY"
    login_hint = "`codex login` 을 재실행하세요."

    def __init__(
        self,
        *,
        sandbox: str = "read-only",
        full_auto: bool = False,
        dangerously_bypass: bool = False,
        skip_git_check: bool = True,
        ephemeral: bool = False,
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
        self.add_dirs = list(add_dirs or [])
        self.config_overrides = dict(config_overrides or {})

        # Web search: must use -c tools.web_search=true (not --search, which
        # is only on top-level `codex`, not `codex exec`).
        if self.web_search:
            self.config_overrides.setdefault("tools.web_search", "true")

    @classmethod
    def _discover_bin(cls) -> Optional[str]:
        return find_codex_bin()

    @classmethod
    def _install_hint(cls) -> str:
        return "`brew install codex` 또는 `npm i -g @openai/codex`."

    def _common_flags(self, model: Optional[str], streaming: bool) -> list[str]:
        args = ["--json"] if streaming else ["--json"]
        # Codex only supports --json for both streaming and non-streaming; we
        # aggregate events ourselves for non-streaming.
        if self.skip_git_check:
            args += ["--skip-git-repo-check"]
        if self.ephemeral:
            args += ["--ephemeral"]
        args += ["-m", model or self.model]
        if self.full_auto:
            args += ["--full-auto"]
        elif self.dangerously_bypass:
            args += ["--dangerously-bypass-approvals-and-sandbox"]
        else:
            if self.sandbox:
                args += ["-s", self.sandbox]
        for k, v in self.config_overrides.items():
            args += ["-c", f"{k}={v}"]
        return args

    def _build_args(
        self,
        prompt: str,
        *,
        session_id: Optional[str],
        resume_last: bool,
        model: Optional[str],
        streaming: bool,
    ) -> list[str]:
        argv: list[str] = [self.bin_path, "exec"]

        if session_id or resume_last:
            # `resume` subcommand: no -s / --add-dir / -C
            argv += ["resume", "--json"]
            if self.skip_git_check:
                argv += ["--skip-git-repo-check"]
            if self.ephemeral:
                argv += ["--ephemeral"]
            argv += ["-m", model or self.model]
            if self.dangerously_bypass:
                argv += ["--dangerously-bypass-approvals-and-sandbox"]
            for k, v in self.config_overrides.items():
                argv += ["-c", f"{k}={v}"]
            if resume_last:
                argv += ["--last"]
            else:
                argv += [session_id]  # type: ignore[list-item]
            argv += [prompt]
            return argv

        argv += self._common_flags(model, streaming)
        if self.cwd:
            argv += ["-C", self.cwd]
        for d in self.add_dirs:
            argv += ["--add-dir", d]
        argv += [prompt]
        return argv

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
                message="Codex 턴이 에러로 종료되었습니다.",
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
