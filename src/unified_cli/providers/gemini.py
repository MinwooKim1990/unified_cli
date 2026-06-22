"""Antigravity (`agy`) provider — successor to the Google Gemini CLI.

As of 2026, the `gemini` CLI returns `IneligibleTierError: This client is no
longer supported for Gemini Code Assist for individuals` for personal accounts
and directs users to migrate to the Antigravity suite (https://antigravity.google).
The Antigravity CLI is the binary `agy` (typically at ~/.local/bin/agy).

`agy` differs from the old `gemini -p` in important ways, all handled here:

  * **Plain-text output** — `agy -p "<prompt>"` prints a markdown answer to
    stdout. There is NO `--output-format json|stream-json`; we therefore parse
    stdout as plain text instead of JSON events.
  * **Agentic by default** — `agy` runs shell/file/web tools on its own and
    decides when to web-search (there is no per-call web-search flag). We pass
    `--dangerously-skip-permissions` so unattended headless calls don't block
    on tool-approval prompts.
  * **Sessions** — `--continue`/`-c` resumes the most recent conversation;
    `--conversation <UUID>` resumes a specific one. Conversations are stored as
    `~/.gemini/antigravity-cli/conversations/<UUID>.db` (SQLite); the newest by
    mtime is the one just used, which is how we recover a session id.
  * **Models** — `agy models` lists human names ("Gemini 3.5 Flash (Medium)",
    "Claude Sonnet 4.6 (Thinking)", "GPT-OSS 120B (Medium)", ...). `--model`
    accepts both those display names and slugs like `gemini-3.5-flash`. Unknown
    model names silently fall back to the default (no error).
  * **Images** — `@/path/to/img.png` references in the prompt + skip-permissions.

The provider key stays `"gemini"` for backward compatibility (route regex,
factory registration, server/CLI surfaces, docs).
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import threading
import time
from typing import AsyncIterator, Iterator, Optional

from ..base import BaseProvider
from ..core import Message, Response, Usage
from ..discovery import find_agy_bin
from ..errors import UnifiedError, classify


# The legacy `gemini` CLI prints this when an individual account is blocked.
# Distinctive enough on its own; matched case-insensitively.
_INELIGIBLE_RE = re.compile(
    r"IneligibleTier|no longer supported for Gemini Code Assist", re.I
)


class GeminiProvider(BaseProvider):
    name = "gemini"
    default_model = "gemini-3.5-flash"
    api_key_env = "GEMINI_API_KEY"   # agy uses OAuth; kept for base fallback API
    login_hint = "`agy` 를 실행해 브라우저로 로그인하세요 (Antigravity)."

    def __init__(
        self,
        *,
        skip_permissions: bool = True,   # auto-approve tools (unattended use)
        sandbox: bool = False,
        add_dirs: Optional[list[str]] = None,
        conversations_dir: Optional[str] = None,
        **kw,
    ):
        super().__init__(**kw)
        self.skip_permissions = skip_permissions
        self.sandbox = sandbox
        self.add_dirs = list(add_dirs or [])
        self._conv_dir = conversations_dir or os.path.expanduser(
            "~/.gemini/antigravity-cli/conversations"
        )
        # agy runs full agentic loops (shell, web, files) which take longer
        # than a one-shot completion. If the caller didn't pin a timeout, give
        # agy more room than the base default.
        if kw.get("timeout") is None:
            self.timeout = max(self.timeout, 300)
            self.stream_timeout = max(self.stream_timeout, 600)
        # Per-call state lives in thread-local storage so concurrent calls on a
        # shared instance (e.g. the server's threadpool) don't clobber each
        # other's requested session / launch snapshot.
        self._tl = threading.local()

    @classmethod
    def _discover_bin(cls) -> Optional[str]:
        return find_agy_bin()

    @classmethod
    def _install_hint(cls) -> str:
        return ("Antigravity CLI `agy` 를 설치하세요 (https://antigravity.google). "
                "또는 AGY_CLI_PATH 환경변수로 경로 지정.")

    # ----- argv construction -----

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
        # Per-call state (thread-local): requested session + a snapshot of the
        # conversations dir taken just before launch, used to recover the
        # actually-written conversation id afterwards.
        self._tl.pending_session = session_id
        self._tl.launch_time = time.time()
        self._tl.db_snapshot = self._db_mtimes()

        argv: list[str] = [self.bin_path]

        model_str = model or self.model
        if model_str:
            self._validate_model(model_str)
            argv += ["--model", model_str]

        if session_id:
            argv += ["--conversation", session_id]
        elif resume_last:
            argv += ["--continue"]

        if self.skip_permissions:
            argv += ["--dangerously-skip-permissions"]
        if self.sandbox:
            argv += ["--sandbox"]
        for d in self.add_dirs:
            argv += ["--add-dir", d]

        # Align agy's own print timeout with our subprocess timeout window.
        window = int(self.stream_timeout if streaming else self.timeout)
        argv += ["--print-timeout", f"{window}s"]

        full_prompt = self._inject_image_refs(prompt, images)
        argv += ["-p", full_prompt]
        return argv, None

    def _inject_image_refs(self, prompt: str, images) -> str:
        if not images:
            return prompt
        from ..core import normalize_images
        refs = []
        for att in normalize_images(images):
            refs.append(f"@{self._materialize(att)}")
        return " ".join(refs) + " " + prompt

    def _materialize(self, att) -> str:
        if att.path:
            return os.path.abspath(os.path.expanduser(att.path))
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
            raise UnifiedError(
                kind="config", provider="gemini",
                message="agy `@<path>` 는 로컬 파일만 받습니다. URL 은 미리 다운로드하세요.",
            )
        raise UnifiedError(
            kind="config", provider="gemini", message="비어있는 이미지 첨부.",
        )

    # ----- model validation -----

    def _validate_model(self, model_str: str) -> None:
        """Reject clearly-unknown models. agy silently falls back to the default
        for an unknown `--model`, which would otherwise hand the user a
        different model's answer without warning. Only enforced when we have a
        real `agy models` list (source="cache"); skipped if agy is unreachable
        so we never false-reject a brand-new model agy actually supports.
        """
        try:
            from ..models import list_models
            infos = list_models("gemini")
        except Exception:
            return
        have_live = any(getattr(i, "source", "") == "cache" for i in infos)
        if not have_live:
            return
        valid = {i.id for i in infos}
        if model_str not in valid:
            raise UnifiedError(
                kind="model_not_allowed", provider="gemini",
                message=f"agy 모델 '{model_str}' 을 찾을 수 없습니다.",
                hint="`unified-cli models gemini` 로 확인하세요 "
                     "(agy 는 잘못된 모델명을 조용히 default 로 폴백합니다).",
            )

    # ----- session id recovery -----

    def _db_mtimes(self) -> dict:
        """Map of <name>.db → mtime in the conversations dir (best-effort)."""
        out: dict = {}
        try:
            names = [f for f in os.listdir(self._conv_dir) if f.endswith(".db")]
        except OSError:
            return out
        for f in names:
            try:
                out[f] = os.path.getmtime(os.path.join(self._conv_dir, f))
            except OSError:
                continue
        return out

    def _resolve_session_id(self) -> str:
        """Recover the conversation id agy just wrote, using the pre-launch
        snapshot: pick the `.db` that is new or whose mtime advanced past
        launch. If the requested session was the one touched, return it (so a
        valid resume reports the same id); otherwise return the actually-written
        id so base's _check_session_match can flag a silent new-session.
        """
        pending = getattr(self._tl, "pending_session", None)
        launch = getattr(self._tl, "launch_time", 0.0)
        snapshot = getattr(self._tl, "db_snapshot", {}) or {}

        now = self._db_mtimes()
        touched = [
            f for f, m in now.items()
            if f not in snapshot or m > snapshot.get(f, 0.0) or m >= launch
        ]
        if touched:
            newest = max(touched, key=lambda f: now[f])[:-3]
            if pending and any(f[:-3] == pending for f in touched):
                return pending
            return newest
        # Fallback: nothing detected as touched (clock skew / fs quirks).
        if pending:
            return pending
        if now:
            return max(now, key=lambda f: now[f])[:-3]
        return ""

    # ----- response parsing (plain text) -----

    def _check_ineligible(self, text: str) -> None:
        if _INELIGIBLE_RE.search(text):
            raise UnifiedError(
                kind="auth_expired", provider="gemini",
                message="이 클라이언트는 더 이상 지원되지 않습니다 — Antigravity(`agy`)로 마이그레이션됨.",
                hint="`agy` 로 로그인했는지 확인하세요. 구 gemini CLI 는 개인 계정에서 차단됨.",
                cause=text[:300],
            )

    def _parse_json_response(self, text: str, model: str) -> Response:
        # NOTE: method name kept for the BaseProvider contract, but agy emits
        # plain text (markdown), not JSON. The entire stdout IS the answer.
        self._check_ineligible(text)
        body = text.strip()
        if not body:
            raise UnifiedError(
                kind="internal", provider="gemini",
                message="agy 가 빈 응답을 반환했습니다.",
                hint="모델/네트워크 상태를 확인하거나 다시 시도하세요.",
                cause=text[:200],
            )
        return Response(
            text=body,
            session_id=self._resolve_session_id(),
            provider="gemini",
            model=model,
            usage=Usage(),  # agy headless does not report token usage
            messages=[],
            raw=[{"text": text}],
        )

    def _normalize(self, obj: dict) -> Iterator[Message]:
        # Unused: agy produces no JSON event stream. Streaming is handled by the
        # overridden _stream_run / astream below. Required by the ABC.
        yield from ()

    # ----- streaming (plain text, line-buffered) -----

    def _stream_run(
        self, args: list[str], stdin_data: Optional[str] = None
    ) -> Iterator[Message]:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE if stdin_data else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=self.cwd, env=self._env(), bufsize=1,
        )
        if stdin_data and proc.stdin:
            try:
                proc.stdin.write(stdin_data)
                proc.stdin.flush()
                proc.stdin.close()
            except BrokenPipeError:
                pass
        assert proc.stdout is not None
        produced = False
        collected: list[str] = []
        try:
            for line in proc.stdout:
                collected.append(line)
                if line.strip():
                    produced = True
                    yield Message(kind="text", provider="gemini",
                                  text=line, raw={"line": line})
        finally:
            try:
                proc.wait(timeout=self.stream_timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise UnifiedError(
                    kind="network", provider="gemini",
                    message=f"agy 스트림이 {self.stream_timeout}초 안에 끝나지 않음.",
                    hint="긴 에이전트 작업이면 timeout 을 늘리세요.",
                )
            stderr_text = proc.stderr.read() if proc.stderr else ""

        full = "".join(collected)
        if proc.returncode not in (0, None):
            self._check_ineligible(full + "\n" + stderr_text)
            err = classify(self.name, stderr_text, full, proc.returncode)
            err._produced_any = produced  # type: ignore[attr-defined]
            raise err

        self._check_ineligible(full)
        if not full.strip():
            raise UnifiedError(
                kind="internal", provider="gemini",
                message="agy 가 빈 응답을 반환했습니다.",
                hint="모델/네트워크 상태를 확인하거나 다시 시도하세요.",
            )
        sid = self._resolve_session_id()
        if sid:
            yield Message(kind="session", provider="gemini", session_id=sid, raw={})
        yield Message(kind="done", provider="gemini", raw={})

    async def astream(
        self,
        prompt: str,
        *,
        session_id: Optional[str] = None,
        resume_last: bool = False,
        model: Optional[str] = None,
        images: Optional[list] = None,
    ) -> AsyncIterator[Message]:
        # agy has no async event stream; run the blocking call in an executor
        # and surface the result as text → session → done.
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: self.chat(
                prompt, session_id=session_id, resume_last=resume_last,
                model=model, images=images,
            ),
        )
        if resp.text:
            yield Message(kind="text", provider="gemini", text=resp.text, raw={})
        if resp.session_id:
            yield Message(kind="session", provider="gemini",
                          session_id=resp.session_id, raw={})
        yield Message(kind="done", provider="gemini", raw={})

    # ----- session listing -----

    def list_sessions(self) -> list[dict]:
        """List conversations from the agy conversations dir (newest first)."""
        mtimes = self._db_mtimes()  # OSError-guarded per file
        return [
            {"session_id": f[:-3], "mtime": m}
            for f, m in sorted(mtimes.items(), key=lambda kv: kv[1], reverse=True)
        ]
