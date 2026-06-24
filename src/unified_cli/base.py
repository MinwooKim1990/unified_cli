"""BaseProvider ABC with shared subprocess execution, retry, and fallback."""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from typing import AsyncIterator, ClassVar, Iterator, Optional

from .core import Message, ModelInfo, ProviderName, Response, Usage
from .errors import UnifiedError, classify
from .i18n import t
from .usage import tracker as _usage_tracker


# Materialized temp files registered for cleanup on interpreter exit (defense
# in depth; per-call cleanup happens in chat/stream finally blocks).
_GLOBAL_TEMP_FILES: set[str] = set()


@atexit.register
def _cleanup_global_temp_files() -> None:
    for p in list(_GLOBAL_TEMP_FILES):
        try:
            os.unlink(p)
        except OSError:
            pass
    _GLOBAL_TEMP_FILES.clear()


# Max 2 retries (0.5s, 1.5s) for network errors; 1 retry for auth fallback.
_NETWORK_BACKOFF = (0.5, 1.5)

# Default subprocess timeouts. The wrapped CLIs occasionally hang (network
# stalls, OAuth refresh edge cases, etc); without timeouts a REPL or HTTP
# server backed by this wrapper can wedge indefinitely. Override via
# `BaseProvider(timeout=N)` if you need shorter or longer.
DEFAULT_CHAT_TIMEOUT = 120        # seconds — non-streaming
DEFAULT_STREAM_TIMEOUT = 300      # seconds — streaming may take longer for long replies


def _reject_empty_prompt(prompt: str, provider: str) -> None:
    """Raise UnifiedError(kind='config') for empty/whitespace-only prompts.

    Applied to chat() and stream() at entry. Without this, Claude in particular
    produces hallucinated responses for blank input.
    """
    if not prompt or not prompt.strip():
        raise UnifiedError(
            kind="config", provider=provider,  # type: ignore[arg-type]
            message=t("err.base.empty_prompt"),
            hint=t("err.base.empty_prompt.hint"),
        )


def _check_session_match(
    provider: str, requested: Optional[str], got: Optional[str]
) -> None:
    """If the user asked to resume `requested` but we got a different session back,
    raise `not_found` instead of silently continuing in a new conversation.

    Catches the Codex-specific behaviour where `codex exec resume <unknown-uuid>`
    succeeds with a fresh session instead of erroring. Claude/Gemini fail loudly
    or pre-check, so this is only meaningful for Codex in practice — but the
    guard is provider-agnostic for safety.
    """
    if not requested or not got:
        return
    if requested != got:
        raise UnifiedError(
            kind="not_found", provider=provider,  # type: ignore[arg-type]
            message=t("err.base.session_mismatch",
                      requested=requested[:12], got=got[:12]),
            hint=t("err.base.session_mismatch.hint"),
            cause=f"requested={requested} got={got}",
        )


def _drain_into(pipe, sink: list) -> None:
    """Read `pipe` to EOF into `sink` (used as a daemon-thread target to drain
    a child's stderr concurrently with the stdout loop — prevents the classic
    pipe-buffer deadlock where an undrained stderr stalls stdout)."""
    if pipe is None:
        return
    try:
        for chunk in pipe:
            sink.append(chunk)
    except Exception:
        pass


class BaseProvider(ABC):
    """Base class for a single-provider CLI wrapper.

    Subclasses must implement:
      - `_build_args(prompt, session_id, resume_last, model, streaming)` → argv list
      - `_normalize(obj)` → iterator of Message (from raw JSON object)
      - `_parse_response(raw_text)` → Response (for non-streaming `--output-format json`)
      - `_default_env()` → dict of env vars to set (subclass-specific)
    """

    name: ClassVar[ProviderName]
    default_model: ClassVar[str]
    api_key_env: ClassVar[str]       # e.g., "ANTHROPIC_API_KEY"

    @classmethod
    def login_hint(cls) -> str:
        """Localized login recovery hint. Resolved at CALL time (not import
        time) so the active language is honored — subclasses override with a
        `t(...)` lookup. The base default points users at a generic re-login.
        """
        return t("err.hint.install_cli")

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        cwd: Optional[str] = None,
        bin_path: Optional[str] = None,
        extra_env: Optional[dict] = None,
        timeout: Optional[float] = None,
        web_search: bool = True,
    ):
        self.model = model or self.default_model
        self.cwd = cwd
        self.extra_env = extra_env or {}
        # `timeout` semantics: explicit value applies to both modes; `None` →
        # mode-specific defaults (chat 120s, stream 300s).
        self.timeout = timeout if timeout is not None else DEFAULT_CHAT_TIMEOUT
        self.stream_timeout = timeout if timeout is not None else DEFAULT_STREAM_TIMEOUT
        self.web_search = web_search
        # Per-call temp files (e.g. image bytes materialized to disk). Tracked
        # thread-locally and unlinked after each call so the long-running server
        # doesn't leak files. See _register_temp_file / _cleanup_temp_files.
        self._tmp = threading.local()

        resolved = bin_path or self._discover_bin()
        if not resolved:
            raise UnifiedError(
                kind="config", provider=self.name,
                message=t("err.base.no_binary", provider=self.name),
                hint=self._install_hint(),
            )
        self.bin_path = resolved

    # ----- abstract -----

    @classmethod
    @abstractmethod
    def _discover_bin(cls) -> Optional[str]: ...

    @classmethod
    @abstractmethod
    def _install_hint(cls) -> str: ...

    @abstractmethod
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
        """Build (argv, stdin_data) for the subprocess call.

        `stdin_data` is `None` for the typical argv-only case, or a string to
        pipe into the child's stdin. Currently used by Codex, whose CLI reads
        the prompt from stdin when an image (`-i`) is attached. (Claude routes
        images through its Read tool, not stdin; agy uses `@path` in the
        prompt — both return stdin_data=None.)
        """

    @abstractmethod
    def _normalize(self, obj: dict) -> Iterator[Message]: ...

    @abstractmethod
    def _parse_json_response(self, text: str, model: str) -> Response: ...

    # ----- temp file lifecycle -----

    def _reset_temp_files(self) -> None:
        self._tmp.files = []

    def _register_temp_file(self, path: str) -> None:
        """Providers call this when they materialize image bytes/URLs to disk,
        so the file is unlinked after the call completes."""
        files = getattr(self._tmp, "files", None)
        if files is None:
            files = self._tmp.files = []
        files.append(path)
        _GLOBAL_TEMP_FILES.add(path)  # atexit safety net

    def _cleanup_temp_files(self) -> None:
        for p in getattr(self._tmp, "files", None) or []:
            try:
                os.unlink(p)
            except OSError:
                pass
            _GLOBAL_TEMP_FILES.discard(p)
        self._tmp.files = []

    # ----- env + subprocess -----

    def _env(self, fallback_api_key: bool = False) -> dict:
        env = os.environ.copy()
        env.update(self.extra_env)
        if fallback_api_key and self.api_key_env in os.environ:
            env[self.api_key_env] = os.environ[self.api_key_env]
        return env

    def _run(self, args: list[str], stdin_data: Optional[str] = None) -> str:
        """Run subprocess with non-streaming output. Returns stdout on success.

        `stdin_data` (if given) is piped to the child's stdin — used by
        Claude's stream-json image input mode.

        Handles auth-expired fallback (retry once with API key env) and network
        retries (up to 2 with exponential backoff).
        """
        tried_api_fallback = False
        last_err: Optional[UnifiedError] = None

        for attempt in range(len(_NETWORK_BACKOFF) + 1):
            try:
                # When no stdin is supplied we still pass empty input ("")
                # rather than letting the child inherit our stdin — Gemini
                # CLI in particular blocks waiting for stdin input even
                # though `-p` is supplied, which causes the wrapper to hang.
                result = subprocess.run(
                    args, capture_output=True, text=True,
                    input=stdin_data if stdin_data is not None else "",
                    cwd=self.cwd, env=self._env(), timeout=self.timeout,
                )
            except subprocess.TimeoutExpired:
                raise UnifiedError(
                    kind="network", provider=self.name,
                    message=t("err.base.timeout", provider=self.name, timeout=self.timeout),
                    hint=t("err.base.timeout.hint"),
                )
            if result.returncode == 0:
                return result.stdout

            err = classify(self.name, result.stderr, result.stdout, result.returncode)
            last_err = err

            if err.kind == "auth_expired" and not tried_api_fallback:
                if self.api_key_env in os.environ:
                    tried_api_fallback = True
                    args_retry = args
                    try:
                        result = subprocess.run(
                            args_retry, capture_output=True, text=True,
                            input=stdin_data if stdin_data is not None else "",
                            cwd=self.cwd, env=self._env(fallback_api_key=True),
                            timeout=self.timeout,
                        )
                    except subprocess.TimeoutExpired:
                        raise UnifiedError(
                            kind="network", provider=self.name,
                            message=t("err.base.timeout_fallback", provider=self.name),
                            hint=t("err.base.timeout_fallback.hint"),
                        )
                    if result.returncode == 0:
                        return result.stdout
                    err = classify(self.name, result.stderr, result.stdout, result.returncode)
                    last_err = err
                raise err  # no key available or fallback also failed

            if err.kind == "network" and attempt < len(_NETWORK_BACKOFF):
                time.sleep(_NETWORK_BACKOFF[attempt])
                continue

            raise err

        assert last_err is not None
        raise last_err

    # ----- public API -----

    def chat(
        self,
        prompt: str,
        *,
        session_id: Optional[str] = None,
        resume_last: bool = False,
        model: Optional[str] = None,
        images: Optional[list] = None,
    ) -> Response:
        _reject_empty_prompt(prompt, self.name)
        self._reset_temp_files()
        args, stdin_data = self._build_args(
            prompt, session_id=session_id, resume_last=resume_last,
            model=model, streaming=False, images=images,
        )
        t0 = time.time()
        try:
            try:
                stdout = self._run(args, stdin_data=stdin_data)
                resp = self._parse_json_response(stdout, model or self.model)
                _check_session_match(self.name, session_id, resp.session_id)
            except UnifiedError as e:
                _usage_tracker.record(
                    self.name, model or self.model,
                    latency_ms=int((time.time() - t0) * 1000),
                    prompt_preview=prompt, error_kind=e.kind,
                )
                raise
            _usage_tracker.record(
                self.name, resp.model,
                input_tokens=resp.usage.input_tokens or 0,
                output_tokens=resp.usage.output_tokens or 0,
                cached_tokens=resp.usage.cached_tokens or 0,
                latency_ms=int((time.time() - t0) * 1000),
                session_id=resp.session_id,
                prompt_preview=prompt,
            )
            return resp
        finally:
            self._cleanup_temp_files()

    def stream(
        self,
        prompt: str,
        *,
        session_id: Optional[str] = None,
        resume_last: bool = False,
        model: Optional[str] = None,
        images: Optional[list] = None,
    ) -> Iterator[Message]:
        _reject_empty_prompt(prompt, self.name)
        self._reset_temp_files()
        args, stdin_data = self._build_args(
            prompt, session_id=session_id, resume_last=resume_last,
            model=model, streaming=True, images=images,
        )
        t0 = time.time()
        final_usage = Usage()
        final_session = ""
        session_checked = False
        try:
            try:
                for msg in self._stream_run(args, stdin_data=stdin_data):
                    if msg.kind == "usage" and msg.usage:
                        final_usage = msg.usage
                    if msg.kind == "session" and msg.session_id:
                        final_session = msg.session_id
                        if not session_checked:
                            _check_session_match(self.name, session_id, msg.session_id)
                            session_checked = True
                    yield msg
            except UnifiedError as e:
                _usage_tracker.record(
                    self.name, model or self.model,
                    latency_ms=int((time.time() - t0) * 1000),
                    prompt_preview=prompt, error_kind=e.kind,
                )
                raise
            _usage_tracker.record(
                self.name, model or self.model,
                input_tokens=final_usage.input_tokens or 0,
                output_tokens=final_usage.output_tokens or 0,
                cached_tokens=final_usage.cached_tokens or 0,
                latency_ms=int((time.time() - t0) * 1000),
                session_id=final_session,
                prompt_preview=prompt,
            )
        finally:
            self._cleanup_temp_files()

    async def achat(self, prompt: str, **kw) -> Response:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self.chat(prompt, **kw))

    async def astream(
        self,
        prompt: str,
        *,
        session_id: Optional[str] = None,
        resume_last: bool = False,
        model: Optional[str] = None,
        images: Optional[list] = None,
    ) -> AsyncIterator[Message]:
        _reject_empty_prompt(prompt, self.name)
        self._reset_temp_files()
        args, stdin_data = self._build_args(
            prompt, session_id=session_id, resume_last=resume_last,
            model=model, streaming=True, images=images,
        )
        t0 = time.time()
        final_usage = Usage()
        final_session = ""
        session_checked = False
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin_data else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd, env=self._env(),
        )
        if stdin_data and proc.stdin:
            proc.stdin.write(stdin_data.encode())
            await proc.stdin.drain()
            proc.stdin.close()
        assert proc.stdout is not None
        # Drain stderr concurrently so a chatty child can't fill the stderr pipe
        # and stall the stdout reader (pipe-buffer deadlock).
        _stderr_chunks: list[bytes] = []

        async def _drain_stderr():
            if proc.stderr is None:
                return
            try:
                async for chunk in proc.stderr:
                    _stderr_chunks.append(chunk)
            except Exception:
                pass

        _drain_task = asyncio.ensure_future(_drain_stderr())
        try:
            async for raw in proc.stdout:
                line = raw.decode().strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for msg in self._normalize(obj):
                    if msg.kind == "usage" and msg.usage:
                        final_usage = msg.usage
                    if (msg.kind == "session" and msg.session_id
                            and not session_checked):
                        final_session = msg.session_id
                        _check_session_match(self.name, session_id, msg.session_id)
                        session_checked = True
                    yield msg
            await proc.wait()
            if proc.returncode != 0:
                try:
                    await asyncio.wait_for(_drain_task, timeout=5)
                except Exception:
                    pass
                err_bytes = b"".join(_stderr_chunks)
                err = classify(self.name, err_bytes.decode(), "", proc.returncode)
                # Mirror sync stream(): record the error turn before raising.
                _usage_tracker.record(
                    self.name, model or self.model,
                    latency_ms=int((time.time() - t0) * 1000),
                    prompt_preview=prompt, error_kind=err.kind,
                )
                raise err
            # Success — record usage parity with sync stream().
            _usage_tracker.record(
                self.name, model or self.model,
                input_tokens=final_usage.input_tokens or 0,
                output_tokens=final_usage.output_tokens or 0,
                cached_tokens=final_usage.cached_tokens or 0,
                latency_ms=int((time.time() - t0) * 1000),
                session_id=final_session,
                prompt_preview=prompt,
            )
        finally:
            # Abort/error mid-stream: kill a still-running child rather than
            # awaiting it (an agentic child may never exit on its own).
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass
            if not _drain_task.done():
                _drain_task.cancel()
            try:
                await _drain_task
            except (asyncio.CancelledError, Exception):
                pass
            self._cleanup_temp_files()

    def _stream_once(
        self,
        args: list[str],
        *,
        fallback: bool,
        stdin_data: Optional[str] = None,
    ) -> Iterator[Message]:
        """Run subprocess once, yield normalized messages, raise on failure."""
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE if stdin_data else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=self.cwd,
            env=self._env(fallback_api_key=fallback), bufsize=1,
        )
        if stdin_data and proc.stdin:
            try:
                proc.stdin.write(stdin_data)
                proc.stdin.flush()
                proc.stdin.close()
            except BrokenPipeError:
                pass
        assert proc.stdout is not None
        # Drain stderr concurrently: if the child writes more than the OS pipe
        # buffer (~64 KB) to stderr while still streaming stdout, an undrained
        # stderr pipe blocks the child, which stalls our stdout loop forever.
        _stderr_chunks: list[str] = []
        _stderr_thread = threading.Thread(
            target=_drain_into, args=(proc.stderr, _stderr_chunks), daemon=True)
        _stderr_thread.start()
        produced_any = False
        loop_done = False
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for msg in self._normalize(obj):
                    produced_any = True
                    yield msg
            loop_done = True
        finally:
            # Aborted mid-stream (generator .close()/error): don't wait on a
            # possibly long-running child — kill it.
            if not loop_done and proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=self.stream_timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise UnifiedError(
                    kind="network", provider=self.name,
                    message=t("err.base.stream_timeout", provider=self.name,
                              timeout=self.stream_timeout),
                    hint=t("err.base.stream_timeout.hint"),
                )
            _stderr_thread.join(timeout=5)
            stderr_text = "".join(_stderr_chunks)

        if proc.returncode not in (0, None):
            err = classify(self.name, stderr_text, "", proc.returncode)
            # attach a marker so the outer retry loop can decide
            err._produced_any = produced_any  # type: ignore[attr-defined]
            raise err

    def _stream_run(
        self, args: list[str], stdin_data: Optional[str] = None
    ) -> Iterator[Message]:
        """Sync streaming with one auth-fallback retry on pre-stream failure."""
        try:
            yield from self._stream_once(args, fallback=False, stdin_data=stdin_data)
            return
        except UnifiedError as err:
            produced = getattr(err, "_produced_any", False)
            if (err.kind == "auth_expired"
                    and not produced
                    and self.api_key_env in os.environ):
                yield from self._stream_once(args, fallback=True, stdin_data=stdin_data)
                return
            raise
