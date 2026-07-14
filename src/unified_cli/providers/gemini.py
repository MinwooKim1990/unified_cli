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
    decides when to web-search (there is no per-call web-search flag). Tool
    approvals remain enabled unless the caller explicitly requests the risky
    `skip_permissions=True` option.
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

from ..base import (
    BaseProvider,
    _StreamReader,
    _drain_into,
    _popen_process_group_kwargs,
    _terminate_process_tree,
)
from ..core import Message, Response, Usage
from ..discovery import find_agy_bin
from ..errors import UnifiedError, classify
from ..i18n import t


# The legacy `gemini` CLI prints this when an individual account is blocked.
# Distinctive enough on its own; matched case-insensitively.
_INELIGIBLE_RE = re.compile(
    r"IneligibleTier|no longer supported for Gemini Code Assist", re.I
)


# ---- opt-in gate -----------------------------------------------------------
# Automating Antigravity (`agy`) can violate Google's Terms of Service, and
# Google has actually banned individual accounts (and cascaded the block across
# Gemini CLI / Code Assist) for it. Unlike Claude Code / Codex headless use —
# which the vendors officially support — agy is high-risk, so this provider is
# DISABLED BY DEFAULT. A user who deliberately wants it must opt in by setting
# the env var below. claude / codex are unaffected.
_ENABLE_ENV = "UNIFIED_CLI_ENABLE_GEMINI"
_TRUTHY = {"1", "true", "yes", "on"}


def gemini_enabled() -> bool:
    """True iff the user has explicitly opted into the agy/gemini provider."""
    return os.environ.get(_ENABLE_ENV, "").strip().lower() in _TRUTHY


def _require_gemini_enabled() -> None:
    if gemini_enabled():
        return
    raise UnifiedError(
        kind="config", provider="gemini",
        message=t("err.gemini.gate_msg"),
        # `{env}` substitutes the literal env var name so the opt-in hint always
        # names UNIFIED_CLI_ENABLE_GEMINI in every language (test_gate asserts it).
        hint=t("err.gemini.gate_hint", env=_ENABLE_ENV),
    )


class GeminiProvider(BaseProvider):
    name = "gemini"
    default_model = "gemini-3.5-flash"
    api_key_env = "GEMINI_API_KEY"   # retained for status/UI compatibility
    allow_api_key_fallback = False    # agy is OAuth-only; never inject this key

    @classmethod
    def login_hint(cls) -> str:
        return t("err.gemini.login_hint")

    def __init__(
        self,
        *,
        skip_permissions: bool = False,  # explicit opt-in only
        sandbox: bool = False,
        add_dirs: Optional[list[str]] = None,
        conversations_dir: Optional[str] = None,
        **kw,
    ):
        # Block construction (chat/stream/server/CLI all funnel through here)
        # unless the user has explicitly opted in. Checked BEFORE super() so the
        # ToS message — not a "binary not found" error — is what users see.
        _require_gemini_enabled()
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
        # agy runs agentic work (shell/web/files) and may emit nothing for a
        # while before its first line, so the short first-output watchdog would
        # kill legitimate runs. Bound streaming only by the overall idle timeout.
        self.first_output_timeout = self.stream_timeout
        # Per-call state lives in thread-local storage so concurrent calls on a
        # shared instance (e.g. the server's threadpool) don't clobber each
        # other's requested session / launch snapshot.
        self._tl = threading.local()

    @classmethod
    def _discover_bin(cls) -> Optional[str]:
        return find_agy_bin()

    @classmethod
    def _install_hint(cls) -> str:
        return t("err.gemini.install_hint")

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
                message=t("err.gemini.image_url_only"),
            )
        raise UnifiedError(
            kind="config", provider="gemini", message=t("err.gemini.empty_image"),
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
                message=t("err.gemini.model_not_found", model=model_str),
                hint=t("err.gemini.model_not_found.hint"),
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
                message=t("err.gemini.ineligible"),
                hint=t("err.gemini.ineligible.hint"),
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
                message=t("err.gemini.empty_response"),
                hint=t("err.gemini.empty_response.hint"),
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
            stdin=subprocess.PIPE if stdin_data else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            cwd=self.cwd, env=self._env(), bufsize=1,
            **_popen_process_group_kwargs(),
        )
        if stdin_data and proc.stdin:
            try:
                proc.stdin.write(stdin_data)
                proc.stdin.flush()
                proc.stdin.close()
            except BrokenPipeError:
                pass
        assert proc.stdout is not None
        # Drain stderr concurrently (pipe-deadlock guard — see base._stream_once).
        _stderr_chunks: list[str] = []
        _stderr_overflow = threading.Event()
        _stderr_thread = threading.Thread(
            target=_drain_into,
            kwargs={
                "pipe": proc.stderr,
                "sink": _stderr_chunks,
                "max_bytes": self.max_stderr_bytes,
                "overflow": _stderr_overflow,
                "terminate": lambda: _terminate_process_tree(proc),
            },
            daemon=True,
        )
        _stderr_thread.start()
        # Read stdout on a background thread with an output watchdog (kills a
        # child that stays silent past the deadline, without penalizing a slow
        # consumer). first_output_timeout == stream_timeout for agy.
        reader = _StreamReader(
            proc,
            first_output=self.first_output_timeout,
            idle=self.stream_timeout,
            max_buffer_bytes=self.max_stream_buffer_bytes,
            max_output_bytes=self.max_output_bytes,
            max_events=self.max_stream_events,
            max_line_bytes=self.max_stream_line_bytes,
            terminate=lambda: _terminate_process_tree(proc),
        ).start()
        produced = False
        collected: list[str] = []
        loop_done = False
        try:
            for line in reader:
                collected.append(line)
                if line.strip():
                    produced = True
                    yield Message(kind="text", provider="gemini",
                                  text=line, raw={"line": line})
            loop_done = True
        finally:
            reader.close()
            # Aborted (Ctrl+C / generator close): agy is agentic and may run for
            # a long time, and stream_timeout is forced to >=600s — so kill
            # rather than wait it out.
            if not loop_done:
                _terminate_process_tree(proc, force_group=True)
            try:
                proc.wait(timeout=self.stream_timeout)
            except subprocess.TimeoutExpired:
                _terminate_process_tree(proc)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                raise UnifiedError(
                    kind="network", provider="gemini",
                    message=t("err.gemini.stream_timeout", timeout=self.stream_timeout),
                    hint=t("err.gemini.stream_timeout.hint"),
                )
            _terminate_process_tree(proc, force_group=True)
            _stderr_thread.join(timeout=5)
            stderr_text = "".join(_stderr_chunks)

        full = "".join(collected)
        if reader.overflow_reason:
            raise self._output_limit_error(reader.overflow_reason)
        if _stderr_overflow.is_set():
            raise self._output_limit_error("stderr")
        if reader.fired:
            raise self._hang_error(before_output=reader.fired_before_output)
        if proc.returncode not in (0, None):
            self._check_ineligible(full + "\n" + stderr_text)
            err = classify(self.name, stderr_text, full, proc.returncode)
            err._produced_any = produced  # type: ignore[attr-defined]
            raise err

        self._check_ineligible(full)
        if not full.strip():
            raise UnifiedError(
                kind="internal", provider="gemini",
                message=t("err.gemini.empty_response"),
                hint=t("err.gemini.empty_response.hint"),
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
