"""BaseProvider ABC with shared subprocess execution, retry, and fallback."""

from __future__ import annotations

import asyncio
import atexit
import contextvars
import json
import os
import queue
import selectors
import signal
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, ClassVar, Iterator, Optional, Union

from .core import Message, ModelInfo, ProviderId, Response, Usage
from .errors import UnifiedError, classify
from .i18n import t
from .usage import tracker as _usage_tracker


# Materialized temp files registered for cleanup on interpreter exit (defense
# in depth; per-call cleanup happens in chat/stream finally blocks).
_GLOBAL_TEMP_FILES: set[str] = set()
_GLOBAL_TEMP_FILES_LOCK = threading.Lock()


@dataclass
class _TempFileScope:
    """Exact attachment files owned by one public provider invocation."""

    files: list[str] = field(default_factory=list)


@atexit.register
def _cleanup_global_temp_files() -> None:
    with _GLOBAL_TEMP_FILES_LOCK:
        files = list(_GLOBAL_TEMP_FILES)
        _GLOBAL_TEMP_FILES.clear()
    for p in files:
        try:
            os.unlink(p)
        except OSError:
            pass


# Max 2 retries (0.5s, 1.5s) for network errors; 1 retry for auth fallback.
_NETWORK_BACKOFF = (0.5, 1.5)

# Default subprocess timeouts. The wrapped CLIs occasionally hang (network
# stalls, OAuth refresh edge cases, etc); without timeouts a REPL or HTTP
# server backed by this wrapper can wedge indefinitely. Override via
# `BaseProvider(timeout=N)` if you need shorter or longer.
DEFAULT_CHAT_TIMEOUT = 120        # seconds — non-streaming
DEFAULT_STREAM_TIMEOUT = 300      # seconds — streaming may take longer for long replies
# Max wait for the FIRST streamed line. claude/codex emit an init event almost
# immediately, so a long gap before any output means the child is wedged — most
# often `claude` blocked on a Keychain-protected OAuth read under launchd/cron
# (no TTY to unlock the Keychain). Kept short so that hang fails fast with an
# actionable message instead of blocking for the full stream timeout. Providers
# whose first token is legitimately slow (agy) raise this in their __init__.
DEFAULT_FIRST_OUTPUT_TIMEOUT = 60

# Hard ceilings prevent a malformed or hostile CLI stream from growing process
# memory without bound. Public callers can explicitly raise them per provider
# for a trusted workload; the server has additional request/response limits.
DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_STDERR_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_STREAM_BUFFER_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_STREAM_EVENTS = 50_000
DEFAULT_MAX_STREAM_LINE_BYTES = 1 * 1024 * 1024

# After the direct provider process exits, pipe EOF can be delayed forever by
# an inherited descriptor in a detached descendant. Give already-written bytes
# one short, absolute grace window, then close our read side.
_PIPE_EXIT_GRACE = 0.1
_PIPE_POLL_INTERVAL = 0.05


class _ProcessTimedOut(Exception):
    pass


class _ProcessCancelled(Exception):
    pass


class _ProcessOutputLimit(Exception):
    def __init__(self, source: str):
        self.source = source


class _ProcessPipeError(Exception):
    def __init__(self, source: str, cause: BaseException):
        self.source = source
        self.cause = cause


def _popen_process_group_kwargs() -> dict:
    """Spawn headless provider CLIs in their own POSIX process group."""
    if os.name == "posix":
        return {"start_new_session": True}
    # Windows child-tree termination needs taskkill or a Job Object. Keep the
    # existing direct-child behavior there rather than claiming unsupported
    # tree semantics.
    return {}


def _process_is_running(proc) -> bool:
    poll = getattr(proc, "poll", None)
    if poll is not None:
        try:
            return poll() is None
        except (ProcessLookupError, OSError):
            return False
    get_returncode = getattr(proc, "get_returncode", None)
    if get_returncode is not None:
        try:
            return get_returncode() is None
        except (ProcessLookupError, OSError):
            return False
    return getattr(proc, "returncode", None) is None


def _cancel_requested(cancel_event: Optional[threading.Event]) -> bool:
    """Read an optional cooperative-cancellation flag defensively."""
    if cancel_event is None:
        return False
    try:
        return bool(cancel_event.is_set())
    except Exception:
        # A caller-supplied flag that cannot be read must never weaken the
        # ordinary subprocess lifecycle or turn into an arbitrary exception.
        return False


def _cancelled_error(provider: str) -> UnifiedError:
    error = UnifiedError(
        kind="internal", provider=provider,  # type: ignore[arg-type]
        message="Provider request was cancelled.",
    )
    # Private marker for trusted boundaries such as the browser manage API.
    # Public Message/Response ABI remains unchanged.
    error._cancelled = True  # type: ignore[attr-defined]
    return error


def _terminate_process_tree(proc, *, force_group: bool = False) -> None:
    """Force-stop a provider child and its dedicated POSIX process group.

    On an abort, the direct CLI leader may already have exited after spawning a
    descendant that inherited its process group. Force-group cleanup is used
    only for that short-lived, wrapper-owned subprocess lifecycle so those
    descendants cannot outlive a cancelled request.
    """
    if os.name == "posix":
        pid = getattr(proc, "pid", None)
        if pid is None:
            get_pid = getattr(proc, "get_pid", None)
            if get_pid is not None:
                try:
                    pid = get_pid()
                except (ProcessLookupError, OSError):
                    pid = None
        if pid and (force_group or _process_is_running(proc)):
            try:
                os.killpg(pid, signal.SIGKILL)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass
    if not _process_is_running(proc):
        return
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


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


def _bytes_len(value: Union[str, bytes]) -> int:
    return len(value) if isinstance(value, bytes) else len(value.encode("utf-8", "replace"))


def _prefix_within_bytes(value: Union[str, bytes], limit: int) -> Union[str, bytes]:
    if isinstance(value, bytes):
        return value[:limit]
    return value.encode("utf-8", "replace")[:limit].decode("utf-8", "ignore")


def _drain_into(
    pipe,
    sink: list,
    *,
    max_bytes: Optional[int] = None,
    overflow: Optional[threading.Event] = None,
    terminate: Optional[Callable[[], None]] = None,
) -> None:
    """Drain a text or bytes pipe with an optional bounded diagnostic tail."""
    if pipe is None:
        return
    total = 0
    try:
        while True:
            chunk = pipe.read(64 * 1024)
            if not chunk:
                return
            size = _bytes_len(chunk)
            if max_bytes is not None and total + size > max_bytes:
                remaining = max(0, max_bytes - total)
                if remaining:
                    # Keep a bounded prefix for error classification; never
                    # retain the overflowing remainder in process memory.
                    sink.append(_prefix_within_bytes(chunk, remaining))
                if overflow is not None:
                    overflow.set()
                if terminate is not None:
                    terminate()
                return
            sink.append(chunk)
            total += size
    except Exception:
        pass


def _drain_binary_into(
    pipe,
    sink: list[bytes],
    *,
    max_bytes: int,
    overflow: threading.Event,
    source: list[str],
    source_name: str,
    terminate: Callable[[], None],
    stop: Optional[threading.Event] = None,
    process_exited: Optional[Callable[[], bool]] = None,
    failure: Optional[list[BaseException]] = None,
) -> None:
    """Binary capture used by non-streaming chat with a strict byte ceiling."""
    if pipe is None:
        return
    total = 0

    def retain(chunk: bytes) -> bool:
        nonlocal total
        if total + len(chunk) > max_bytes:
            remaining = max(0, max_bytes - total)
            if remaining:
                sink.append(chunk[:remaining])
            source.append(source_name)
            overflow.set()
            terminate()
            return False
        sink.append(chunk)
        total += len(chunk)
        return True

    try:
        if os.name == "posix":
            # BufferedReader.read() can hold its internal lock indefinitely
            # when an unrelated detached descendant inherits the pipe.  Read
            # the descriptor directly instead: select makes the loop
            # stop-aware and os.read avoids the buffered object's lock.  Once
            # the direct child exits, drain only bytes already available and
            # return even if another process still owns a write descriptor.
            try:
                fd = pipe.fileno()
            except (AttributeError, OSError, ValueError):
                fd = None
            if fd is not None:
                try:
                    os.set_blocking(fd, False)
                except OSError:
                    # A readiness selector + os.read remains bounded on a
                    # readable pipe even if O_NONBLOCK is unavailable.
                    pass
                with selectors.DefaultSelector() as selector:
                    selector.register(fd, selectors.EVENT_READ)
                    exit_deadline: Optional[float] = None
                    while stop is None or not stop.is_set():
                        now = time.monotonic()
                        exited = bool(process_exited and process_exited())
                        if exited and exit_deadline is None:
                            exit_deadline = now + _PIPE_EXIT_GRACE
                        if exit_deadline is not None and now >= exit_deadline:
                            return
                        timeout = _PIPE_POLL_INTERVAL
                        if exit_deadline is not None:
                            timeout = min(timeout, exit_deadline - now)
                        if not selector.select(timeout):
                            continue
                        if (not exited and process_exited is not None
                                and process_exited()):
                            exited = True
                            if exit_deadline is None:
                                exit_deadline = (
                                    time.monotonic() + _PIPE_EXIT_GRACE)
                        try:
                            chunk = os.read(fd, 64 * 1024)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            return
                        if total + len(chunk) > max_bytes and exited:
                            # Bytes arriving only after the provider leader has
                            # exited belong to an inherited descriptor holder,
                            # not the completed provider response. Cut off
                            # without turning a successful response into a
                            # detached-child resource-limit failure.
                            remaining = max(0, max_bytes - total)
                            if remaining:
                                sink.append(chunk[:remaining])
                            return
                        if not retain(chunk):
                            return
                return

        while True:
            chunk = pipe.read(64 * 1024)
            if not chunk:
                return
            if not retain(chunk):
                return
    except Exception as exc:
        if failure is not None:
            failure.append(exc)
            terminate()
    finally:
        # The reader owns this file object and closes it from the same thread,
        # after all descriptor reads have stopped. This avoids both buffered
        # lock races and leaks in shared callers such as manage verification.
        try:
            pipe.close()
        except (OSError, ValueError):
            pass


def _close_popen_pipes(proc, *, skip: tuple = ()) -> None:
    """Close wrapper-owned ``subprocess.Popen`` pipes without masking errors."""
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is None or pipe in skip or getattr(pipe, "closed", False):
            continue
        try:
            pipe.close()
        except (OSError, ValueError):
            pass


class _AsyncJsonlProtocol(asyncio.SubprocessProtocol):
    """Bounded public-API subprocess protocol for async JSONL streaming.

    ``asyncio.Process.wait()`` is coupled to pipe EOF and can therefore wait
    forever when a detached descendant inherits stdout/stderr. The low-level
    public protocol API reports leader exit independently via ``process_exited``.
    That callback starts one absolute grace window for already-written pipe data;
    the public transport is closed at the cutoff even if bytes keep arriving.
    """

    def __init__(
        self,
        loop,
        *,
        max_output_bytes: int,
        max_stderr_bytes: int,
        max_buffer_bytes: int,
        max_events: int,
        max_line_bytes: int,
    ):
        self.loop = loop
        self.max_output_bytes = max_output_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.max_buffer_bytes = max_buffer_bytes
        self.max_events = max_events
        self.max_line_bytes = max_line_bytes
        self.transport = None
        self.queue: "asyncio.Queue" = asyncio.Queue()
        self.process_exited_event = asyncio.Event()
        self.connection_lost_event = asyncio.Event()
        self.stdin_writable = asyncio.Event()
        self.stdin_writable.set()
        self.returncode: Optional[int] = None
        self.stdout_pending = bytearray()
        self.stderr_chunks: list[bytes] = []
        self.stderr_size = 0
        self.total_bytes = 0
        self.buffered_bytes = 0
        self.event_count = 0
        self.produced = False
        self.last_output = time.monotonic()
        self.overflow_reason = ""
        self.read_error: Optional[BaseException] = None
        self.leader_exited = False
        self.stdout_finished = False
        self.cutoff_started = False
        self._cutoff_handle = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def pause_writing(self) -> None:
        self.stdin_writable.clear()

    def resume_writing(self) -> None:
        self.stdin_writable.set()

    def process_exited(self) -> None:
        if self.transport is not None:
            self.returncode = self.transport.get_returncode()
            _terminate_process_tree(self.transport, force_group=True)
        self.leader_exited = True
        self.process_exited_event.set()
        if self._cutoff_handle is None:
            self._cutoff_handle = self.loop.call_later(
                _PIPE_EXIT_GRACE, self._cutoff)

    def pipe_data_received(self, fd: int, data: bytes) -> None:
        if self.cutoff_started:
            return
        try:
            if fd == 1:
                self._stdout_data(data)
            elif fd == 2:
                self._stderr_data(data)
        except Exception as exc:
            self._fail_reader(exc)

    def pipe_connection_lost(self, fd: int, exc) -> None:
        if exc is not None and not self.cutoff_started:
            self._fail_reader(exc)
            return
        if fd == 1:
            self._finish_stdout()

    def connection_lost(self, exc) -> None:
        if self._cutoff_handle is not None:
            self._cutoff_handle.cancel()
            self._cutoff_handle = None
        if exc is not None and self.read_error is None:
            self._fail_reader(exc)
        self._finish_stdout()
        self.connection_lost_event.set()

    def _stdout_data(self, data: bytes) -> None:
        if self.stdout_finished:
            return
        self.stdout_pending.extend(data)
        while True:
            newline = self.stdout_pending.find(b"\n")
            if newline < 0:
                break
            raw = bytes(self.stdout_pending[:newline + 1])
            del self.stdout_pending[:newline + 1]
            if not self._accept_line(raw):
                return
        if len(self.stdout_pending) >= self.max_line_bytes:
            self._limit("line")

    def _stderr_data(self, data: bytes) -> None:
        if self.stderr_size + len(data) > self.max_stderr_bytes:
            if self.leader_exited:
                self._cutoff()
                return
            remaining = max(0, self.max_stderr_bytes - self.stderr_size)
            if remaining:
                self.stderr_chunks.append(data[:remaining])
            self._limit("stderr")
            return
        self.stderr_chunks.append(data)
        self.stderr_size += len(data)

    def _accept_line(self, raw: bytes) -> bool:
        reason = ""
        if (len(raw) > self.max_line_bytes
                or (len(raw) >= self.max_line_bytes
                    and not raw.endswith(b"\n"))):
            reason = "line"
        elif self.total_bytes + len(raw) > self.max_output_bytes:
            reason = "stdout"
        elif self.event_count >= self.max_events:
            reason = "event_count"
        elif self.buffered_bytes + len(raw) > self.max_buffer_bytes:
            reason = "stream_buffer"
        if reason:
            self._limit(reason)
            return False
        self.total_bytes += len(raw)
        self.event_count += 1
        self.buffered_bytes += len(raw)
        self.produced = True
        self.last_output = time.monotonic()
        self.queue.put_nowait(("line", raw))
        return True

    def _limit(self, reason: str) -> None:
        if self.leader_exited:
            # Once the provider leader is gone, additional inherited-pipe data
            # is not provider output. Cut it off without converting a completed
            # provider response into a detached-child limit error.
            self._cutoff()
            return
        if self.overflow_reason:
            return
        self.overflow_reason = reason
        self.queue.put_nowait(("limit", reason))
        if self.transport is not None:
            _terminate_process_tree(self.transport)

    def _fail_reader(self, exc: BaseException) -> None:
        if self.read_error is not None:
            return
        self.read_error = exc
        self.queue.put_nowait(("reader_error", exc))
        if self.transport is not None:
            _terminate_process_tree(self.transport)
        self._cutoff()

    def _finish_stdout(self) -> None:
        if self.stdout_finished:
            return
        self.stdout_finished = True
        if self.stdout_pending:
            raw = bytes(self.stdout_pending)
            self.stdout_pending.clear()
            self._accept_line(raw)
        self.queue.put_nowait(("eof", None))

    def _cutoff(self) -> None:
        if self.cutoff_started:
            return
        self.cutoff_started = True
        self._finish_stdout()
        if self.transport is not None and not self.transport.is_closing():
            self.transport.close()

    def consume_line(self, size: int) -> None:
        self.buffered_bytes = max(0, self.buffered_bytes - size)

    async def close(self) -> None:
        if self._cutoff_handle is not None:
            self._cutoff_handle.cancel()
            self._cutoff_handle = None
        self._cutoff()
        try:
            await asyncio.wait_for(self.connection_lost_event.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            pass


class _StreamReader:
    """Reads a child's stdout on a background thread into a queue while a
    watchdog thread tracks the CHILD's own output cadence.

    Reading on a dedicated thread decouples the child's liveness from the
    *consumer's* pull rate: a slow consumer (e.g. an SSE HTTP client applying
    backpressure, or a per-message loop doing slow work) keeps the reader
    draining the pipe, so the child never blocks on write and is never mistaken
    for a hang. The watchdog kills the child only when IT goes silent past a
    deadline: `first_output` before the very first line (catches a child wedged
    before any output — e.g. `claude` blocked on the login Keychain under
    launchd, with no TTY to unlock it) and `idle` between subsequent lines.

    Iterate for decoded stdout lines (blocks until the next line or, after a
    watchdog kill / child exit, EOF). Afterwards inspect `.fired` /
    `.fired_before_output`, then `close()`.
    """

    _EOF = object()

    def __init__(
        self,
        proc,
        *,
        first_output: float,
        idle: float,
        max_buffer_bytes: int,
        max_output_bytes: int,
        max_events: int,
        max_line_bytes: int,
        terminate: Optional[Callable[[], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ):
        self._proc = proc
        self._first = first_output
        self._idle = idle
        self._q: "queue.Queue" = queue.Queue()
        self._max_buffer_bytes = max_buffer_bytes
        self._max_output_bytes = max_output_bytes
        self._max_events = max_events
        self._max_line_bytes = max_line_bytes
        self._terminate = terminate or (lambda: _terminate_process_tree(proc))
        self._cancel_event = cancel_event
        self._counter_lock = threading.Lock()
        self._buffered_bytes = 0
        self._total_bytes = 0
        self._event_count = 0
        self._last = time.monotonic()   # last time the CHILD emitted a line
        self._produced = False
        self._stop = threading.Event()
        self.fired = False
        self.fired_before_output = False
        self.cancelled = False
        self.overflow_reason = ""
        self.read_error: Optional[BaseException] = None
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._watch = threading.Thread(target=self._watch_loop, daemon=True)

    def start(self) -> "_StreamReader":
        self._reader.start()
        self._watch.start()
        return self

    def _read_loop(self) -> None:
        try:
            if os.name == "posix":
                self._read_posix_loop()
                return
            while True:
                # The size argument stops an unterminated giant JSON line from
                # allocating unbounded memory before we can reject it.
                line = self._proc.stdout.readline(self._max_line_bytes + 1)
                if not line:
                    break
                raw = line if isinstance(line, bytes) else line.encode(
                    "utf-8", "replace")
                if not self._queue_line(raw):
                    break
        except Exception as exc:
            self.read_error = exc
            self._terminate()
        finally:
            self._q.put((self._EOF, 0))

    def _read_posix_loop(self) -> None:
        """Read complete UTF-8 lines without blocking on a buffered object."""
        fd = self._proc.stdout.fileno()
        try:
            os.set_blocking(fd, False)
        except OSError:
            pass
        pending = bytearray()
        exit_deadline: Optional[float] = None
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while not self._stop.is_set():
                now = time.monotonic()
                exited = self._proc.poll() is not None
                if exited and exit_deadline is None:
                    exit_deadline = now + _PIPE_EXIT_GRACE
                if exit_deadline is not None and now >= exit_deadline:
                    break
                timeout = _PIPE_POLL_INTERVAL
                if exit_deadline is not None:
                    timeout = min(timeout, exit_deadline - now)
                if not selector.select(timeout):
                    continue
                if not exited and self._proc.poll() is not None:
                    exited = True
                    if exit_deadline is None:
                        exit_deadline = time.monotonic() + _PIPE_EXIT_GRACE
                try:
                    chunk = os.read(fd, 64 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    break
                pending.extend(chunk)
                while True:
                    newline = pending.find(b"\n")
                    if newline < 0:
                        break
                    raw = bytes(pending[:newline + 1])
                    del pending[:newline + 1]
                    if not self._queue_line(raw, after_exit=exited):
                        return
                # Match BufferedReader.readline(limit + 1): an unterminated
                # line reaching the configured limit is already too long.
                if len(pending) >= self._max_line_bytes:
                    if exited:
                        return
                    self.overflow_reason = "line"
                    self._terminate()
                    return
        if pending and not self._stop.is_set():
            self._queue_line(
                bytes(pending), after_exit=exit_deadline is not None)

    def _queue_line(self, raw: bytes, *, after_exit: bool = False) -> bool:
        """Apply byte/event limits and enqueue one decoded JSONL line."""
        size = len(raw)
        reason = ""
        if (size > self._max_line_bytes
                or (size >= self._max_line_bytes
                    and not raw.endswith(b"\n"))):
            reason = "line"
        with self._counter_lock:
            if not reason:
                if self._total_bytes + size > self._max_output_bytes:
                    reason = "stdout"
                elif self._event_count >= self._max_events:
                    reason = "event_count"
                elif self._buffered_bytes + size > self._max_buffer_bytes:
                    reason = "stream_buffer"
            if reason:
                # Bytes accepted only during the bounded post-leader grace are
                # not provider output. Stop without manufacturing or replacing
                # a pre-exit overflow reason that outer sync/Gemini code would
                # incorrectly surface as resource_limit.
                if after_exit:
                    return False
                if not self.overflow_reason:
                    self.overflow_reason = reason
            else:
                self._total_bytes += size
                self._event_count += 1
                self._buffered_bytes += size
        if reason:
            self._terminate()
            return False
        self._last = time.monotonic()
        self._produced = True
        self._q.put((raw.decode("utf-8", "replace"), size))
        return True

    def _watch_loop(self) -> None:
        while not self._stop.wait(0.05):
            if _cancel_requested(self._cancel_event):
                self.cancelled = True
                self._terminate()
                return
            if self._proc.poll() is not None:
                return  # child exited on its own; reader will emit EOF
            deadline = self._idle if self._produced else self._first
            if time.monotonic() - self._last > deadline:
                self.fired = True
                self.fired_before_output = not self._produced
                self._terminate()
                return

    def __iter__(self):
        while True:
            item, size = self._q.get()
            if item is self._EOF:
                return
            with self._counter_lock:
                self._buffered_bytes = max(0, self._buffered_bytes - size)
            yield item

    def close(self) -> None:
        self._stop.set()
        self._watch.join(timeout=1)

    def wait_closed(self, timeout: float) -> None:
        """Wait until the stdout reader has observed EOF."""
        self._reader.join(timeout=timeout)

    def is_closed(self) -> bool:
        return not self._reader.is_alive()


class BaseProvider(ABC):
    """Base class for a single-provider CLI wrapper.

    Subclasses must implement:
      - `_build_args(prompt, session_id, resume_last, model, streaming)` → argv list
      - `_normalize(obj)` → iterator of Message (from raw JSON object)
      - `_parse_response(raw_text)` → Response (for non-streaming `--output-format json`)
      - `_default_env()` → dict of env vars to set (subclass-specific)
    """

    name: ClassVar[ProviderId]
    default_model: ClassVar[str]
    api_key_env: ClassVar[str]       # e.g., "ANTHROPIC_API_KEY"
    # Some subscription CLIs are OAuth-only. Such providers still expose an
    # API-key environment variable for UI/status compatibility, but must never
    # retry an OAuth failure by injecting that key into a different CLI.
    allow_api_key_fallback: ClassVar[bool] = True

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
        first_output_timeout: Optional[float] = None,
        web_search: bool = True,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        max_stderr_bytes: int = DEFAULT_MAX_STDERR_BYTES,
        max_stream_buffer_bytes: int = DEFAULT_MAX_STREAM_BUFFER_BYTES,
        max_stream_events: int = DEFAULT_MAX_STREAM_EVENTS,
        max_stream_line_bytes: int = DEFAULT_MAX_STREAM_LINE_BYTES,
    ):
        self.model = model or self.default_model
        self.cwd = cwd
        self.extra_env = extra_env or {}
        # `timeout` semantics: explicit value applies to both modes; `None` →
        # mode-specific defaults (chat 120s, stream 300s).
        self.timeout = timeout if timeout is not None else DEFAULT_CHAT_TIMEOUT
        self.stream_timeout = timeout if timeout is not None else DEFAULT_STREAM_TIMEOUT
        # First-line deadline for streaming (see DEFAULT_FIRST_OUTPUT_TIMEOUT).
        # Never exceeds the overall stream timeout.
        self.first_output_timeout = min(
            first_output_timeout if first_output_timeout is not None
            else DEFAULT_FIRST_OUTPUT_TIMEOUT,
            self.stream_timeout,
        )
        self.web_search = web_search
        for name, value in (
            ("max_output_bytes", max_output_bytes),
            ("max_stderr_bytes", max_stderr_bytes),
            ("max_stream_buffer_bytes", max_stream_buffer_bytes),
            ("max_stream_events", max_stream_events),
            ("max_stream_line_bytes", max_stream_line_bytes),
        ):
            if not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self.max_output_bytes = max_output_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.max_stream_buffer_bytes = max_stream_buffer_bytes
        self.max_stream_events = max_stream_events
        self.max_stream_line_bytes = max_stream_line_bytes
        # Materialized attachment files are tied to an explicit invocation
        # scope, not a thread. Async streams can interleave on one event-loop
        # thread, so thread-local tracking could delete another task's image.
        self._temp_scope: "contextvars.ContextVar[Optional[_TempFileScope]]" = (
            contextvars.ContextVar(f"unified_cli_temp_scope_{id(self)}", default=None)
        )

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

    def _new_stream_state(self) -> object:
        """Return parser state owned by one public stream invocation.

        Most providers are stateless while normalizing NDJSON, so the default
        is deliberately opaque. Providers whose wire format emits partial and
        final versions of the same content can override this together with
        ``_stream_normalize``. Keeping it invocation-local avoids sharing
        parser state when callers interleave generators from one provider
        instance.
        """
        return object()

    def _stream_normalize(self, obj: dict, state: object) -> Iterator[Message]:
        """Normalize one streaming event using invocation-local parser state."""
        del state
        yield from self._normalize(obj)

    @abstractmethod
    def _parse_json_response(self, text: str, model: str) -> Response: ...

    # ----- temp file lifecycle -----

    def _new_temp_scope(self) -> _TempFileScope:
        return _TempFileScope()

    def _build_args_in_temp_scope(
        self,
        scope: _TempFileScope,
        prompt: str,
        *,
        session_id: Optional[str],
        resume_last: bool,
        model: Optional[str],
        streaming: bool,
        images: Optional[list],
    ) -> tuple[list[str], Optional[str]]:
        token = self._temp_scope.set(scope)
        try:
            return self._build_args(
                prompt, session_id=session_id, resume_last=resume_last,
                model=model, streaming=streaming, images=images,
            )
        finally:
            # Keep the scope object locally for deterministic cleanup, rather
            # than looking up mutable task/thread ambient state later.
            self._temp_scope.reset(token)

    def _reset_temp_files(self) -> None:
        """Compatibility helper for subclasses that invoke build methods directly."""
        self._temp_scope.set(self._new_temp_scope())

    def _register_temp_file(self, path: str) -> None:
        """Providers call this when they materialize image bytes/URLs to disk,
        so the file is unlinked after the call completes."""
        scope = self._temp_scope.get()
        if scope is None:
            # Preserve direct private build-args use for integrations/tests.
            # Public chat/stream paths always install an explicit scope.
            scope = self._new_temp_scope()
            self._temp_scope.set(scope)
        scope.files.append(path)
        with _GLOBAL_TEMP_FILES_LOCK:
            _GLOBAL_TEMP_FILES.add(path)  # atexit safety net

    def _cleanup_temp_files(self, scope: Optional[_TempFileScope] = None) -> None:
        target = scope or self._temp_scope.get()
        if target is None:
            return
        for p in target.files:
            try:
                os.unlink(p)
            except OSError:
                pass
            with _GLOBAL_TEMP_FILES_LOCK:
                _GLOBAL_TEMP_FILES.discard(p)
        target.files.clear()

    # ----- env + subprocess -----

    def _env(self, fallback_api_key: bool = False) -> dict:
        """Environment for the child CLI.

        The whole point of this wrapper is to run on the user's *subscription*
        OAuth, not per-token API billing. So by default we STRIP any inherited
        vendor API key (e.g. an exported ANTHROPIC_API_KEY) — otherwise the
        wrapped CLI would silently switch to metered API billing and defeat the
        package's core value. Only the explicit auth-expired fallback path
        (`fallback_api_key=True`) keeps the key. A deliberate key passed via
        `extra_env` always wins (applied after the pop).
        """
        env = os.environ.copy()
        if not (fallback_api_key and self.allow_api_key_fallback):
            env.pop(self.api_key_env, None)
        env.update(self.extra_env)
        return env

    # ----- hang / auth diagnosis -----

    def _keychain_block_suspected(self) -> bool:
        """True when a claude hang is most likely a macOS Keychain block.

        `claude` stores OAuth creds in the login Keychain on macOS. Under a
        launchd/cron/daemon context (no TTY to unlock it) the read blocks
        forever. We flag this only when: provider is claude, on darwin, stdin is
        not a TTY (daemon-like), and no file-based credentials exist (so creds
        really are in the Keychain).
        """
        if self.name != "claude" or sys.platform != "darwin":
            return False
        try:
            if sys.stdin is not None and sys.stdin.isatty():
                return False
        except (ValueError, OSError):
            pass  # detached stdin (daemon) → treat as non-interactive
        return not os.path.exists(
            os.path.expanduser("~/.claude/.credentials.json")
        )

    def _hang_error(self, *, before_output: bool) -> UnifiedError:
        """Build the UnifiedError raised when the streaming watchdog kills a
        wedged child. Points macOS/launchd users at the Keychain fix."""
        timeout = self.first_output_timeout if before_output else self.stream_timeout
        if self._keychain_block_suspected():
            hint = t("err.base.keychain_hint")
        else:
            hint = t("err.base.stream_timeout.hint")
        key = "err.base.no_first_output" if before_output else "err.base.stream_timeout"
        return UnifiedError(
            kind="network", provider=self.name,
            message=t(key, provider=self.name, timeout=int(timeout)),
            hint=hint,
        )

    def _run_once(
        self,
        args: list[str],
        stdin_data: Optional[str],
        *,
        fallback_api_key: bool,
        cancel_event: Optional[threading.Event] = None,
    ) -> tuple[str, str, int]:
        """Run one bounded non-streaming child and capture its output safely."""
        if _cancel_requested(cancel_event):
            raise _ProcessCancelled()
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE if stdin_data else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd,
            env=self._env(fallback_api_key=fallback_api_key),
            **_popen_process_group_kwargs(),
        )
        overflow = threading.Event()
        overflow_sources: list[str] = []
        reader_failures: list[BaseException] = []
        terminate = lambda: _terminate_process_tree(proc)
        reader_stop = threading.Event()
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        stdout_thread = threading.Thread(
            target=_drain_binary_into,
            kwargs={
                "pipe": proc.stdout, "sink": stdout_chunks,
                "max_bytes": self.max_output_bytes, "overflow": overflow,
                "source": overflow_sources, "source_name": "stdout",
                "terminate": terminate,
                "stop": reader_stop,
                "process_exited": lambda: proc.poll() is not None,
                "failure": reader_failures,
            },
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain_binary_into,
            kwargs={
                "pipe": proc.stderr, "sink": stderr_chunks,
                "max_bytes": self.max_stderr_bytes, "overflow": overflow,
                "source": overflow_sources, "source_name": "stderr",
                "terminate": terminate,
                "stop": reader_stop,
                "process_exited": lambda: proc.poll() is not None,
                "failure": reader_failures,
            },
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        stdin_thread: Optional[threading.Thread] = None
        if stdin_data and proc.stdin is not None:
            def write_stdin() -> None:
                try:
                    proc.stdin.write(stdin_data.encode("utf-8"))
                    proc.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    pass
                finally:
                    try:
                        proc.stdin.close()
                    except (OSError, ValueError):
                        pass
            stdin_thread = threading.Thread(target=write_stdin, daemon=True)
            stdin_thread.start()

        timed_out = False
        cancelled = False
        deadline = time.monotonic() + self.timeout
        while proc.poll() is None:
            if _cancel_requested(cancel_event):
                terminate()
                cancelled = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                terminate()
                break
            try:
                proc.wait(timeout=min(remaining, 0.1))
            except subprocess.TimeoutExpired:
                continue

        if proc.poll() is None:
            terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        # A completed headless provider call must not leave a background
        # descendant in its dedicated process group.
        _terminate_process_tree(proc, force_group=True)
        for thread in (stdout_thread, stderr_thread):
            thread.join(timeout=0.2)
        reader_stop.set()
        for thread in (stdout_thread, stderr_thread, stdin_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=1)
        # Never close a buffered file object while another thread may hold its
        # lock. POSIX readers above are stop-aware and normally always finish;
        # the skip is a bounded fallback for platforms without pollable pipes.
        live_pipes = []
        if stdout_thread.is_alive():
            live_pipes.append(proc.stdout)
        if stderr_thread.is_alive():
            live_pipes.append(proc.stderr)
        if stdin_thread is not None and stdin_thread.is_alive():
            live_pipes.append(proc.stdin)
        _close_popen_pipes(proc, skip=tuple(live_pipes))

        if cancelled:
            raise _ProcessCancelled()
        if timed_out:
            raise _ProcessTimedOut()
        if overflow.is_set():
            raise _ProcessOutputLimit(overflow_sources[0] if overflow_sources else "output")
        if reader_failures:
            raise _ProcessPipeError("stdout/stderr", reader_failures[0])
        return (
            b"".join(stdout_chunks).decode("utf-8", "replace"),
            b"".join(stderr_chunks).decode("utf-8", "replace"),
            proc.returncode if proc.returncode is not None else -1,
        )

    def _output_limit_error(self, source: str) -> UnifiedError:
        return UnifiedError(
            kind="resource_limit", provider=self.name,
            message=t("err.base.output_limit", provider=self.name),
            hint=t("err.base.output_limit.hint"),
            cause=f"{source} exceeded configured output limit",
        )

    def _pipe_reader_error(
        self, source: str, cause: Optional[BaseException]
    ) -> UnifiedError:
        return UnifiedError(
            kind="internal", provider=self.name,
            message=f"{self.name} subprocess output reader failed.",
            hint=t("err.base.output_limit.hint"),
            cause=f"{source}: {cause!r}",
        )

    def _run(
        self,
        args: list[str],
        stdin_data: Optional[str] = None,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
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
                run_kwargs = {"fallback_api_key": False}
                if cancel_event is not None:
                    run_kwargs["cancel_event"] = cancel_event
                stdout, stderr, returncode = self._run_once(
                    args, stdin_data, **run_kwargs)
            except _ProcessCancelled:
                raise _cancelled_error(self.name)
            except _ProcessTimedOut:
                raise UnifiedError(
                    kind="network", provider=self.name,
                    message=t("err.base.timeout", provider=self.name, timeout=self.timeout),
                    hint=(t("err.base.keychain_hint")
                          if self._keychain_block_suspected()
                          else t("err.base.timeout.hint")),
                )
            except _ProcessOutputLimit as exc:
                raise self._output_limit_error(exc.source)
            except _ProcessPipeError as exc:
                raise self._pipe_reader_error(exc.source, exc.cause)
            if returncode == 0:
                return stdout

            err = classify(self.name, stderr, stdout, returncode)
            last_err = err

            if err.kind == "auth_expired" and not tried_api_fallback:
                if (self.allow_api_key_fallback
                        and self.api_key_env in os.environ):
                    tried_api_fallback = True
                    try:
                        run_kwargs = {"fallback_api_key": True}
                        if cancel_event is not None:
                            run_kwargs["cancel_event"] = cancel_event
                        stdout, stderr, returncode = self._run_once(
                            args, stdin_data, **run_kwargs
                        )
                    except _ProcessCancelled:
                        raise _cancelled_error(self.name)
                    except _ProcessTimedOut:
                        raise UnifiedError(
                            kind="network", provider=self.name,
                            message=t("err.base.timeout_fallback", provider=self.name),
                            hint=t("err.base.timeout_fallback.hint"),
                        )
                    except _ProcessOutputLimit as exc:
                        raise self._output_limit_error(exc.source)
                    except _ProcessPipeError as exc:
                        raise self._pipe_reader_error(exc.source, exc.cause)
                    if returncode == 0:
                        return stdout
                    err = classify(self.name, stderr, stdout, returncode)
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
        cancel_event: Optional[threading.Event] = None,
    ) -> Response:
        _reject_empty_prompt(prompt, self.name)
        scope = self._new_temp_scope()
        try:
            args, stdin_data = self._build_args_in_temp_scope(
                scope, prompt, session_id=session_id, resume_last=resume_last,
                model=model, streaming=False, images=images,
            )
            t0 = time.time()
            try:
                run_kwargs = {"stdin_data": stdin_data}
                if cancel_event is not None:
                    run_kwargs["cancel_event"] = cancel_event
                stdout = self._run(args, **run_kwargs)
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
            self._cleanup_temp_files(scope)

    def stream(
        self,
        prompt: str,
        *,
        session_id: Optional[str] = None,
        resume_last: bool = False,
        model: Optional[str] = None,
        images: Optional[list] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Iterator[Message]:
        _reject_empty_prompt(prompt, self.name)
        scope = self._new_temp_scope()
        try:
            args, stdin_data = self._build_args_in_temp_scope(
                scope, prompt, session_id=session_id, resume_last=resume_last,
                model=model, streaming=True, images=images,
            )
            t0 = time.time()
            final_usage = Usage()
            final_session = ""
            session_checked = False
            try:
                run_kwargs = {"stdin_data": stdin_data}
                if cancel_event is not None:
                    run_kwargs["cancel_event"] = cancel_event
                for msg in self._stream_run(args, **run_kwargs):
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
            self._cleanup_temp_files(scope)

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
        cancel_event: Optional[threading.Event] = None,
    ) -> AsyncIterator[Message]:
        _reject_empty_prompt(prompt, self.name)
        scope = self._new_temp_scope()
        stream_state = self._new_stream_state()
        token = self._temp_scope.set(scope)
        try:
            args, stdin_data = self._build_args(
                prompt, session_id=session_id, resume_last=resume_last,
                model=model, streaming=True, images=images,
            )
        except BaseException:
            self._cleanup_temp_files(scope)
            raise
        finally:
            self._temp_scope.reset(token)
        t0 = time.time()
        final_usage = Usage()
        final_session = ""
        session_checked = False
        transport = None
        protocol: Optional[_AsyncJsonlProtocol] = None
        try:
            if _cancel_requested(cancel_event):
                raise _cancelled_error(self.name)
            loop = asyncio.get_running_loop()
            factory = lambda: _AsyncJsonlProtocol(
                loop,
                max_output_bytes=self.max_output_bytes,
                max_stderr_bytes=self.max_stderr_bytes,
                max_buffer_bytes=self.max_stream_buffer_bytes,
                max_events=self.max_stream_events,
                max_line_bytes=self.max_stream_line_bytes,
            )
            transport, protocol = await loop.subprocess_exec(
                factory, *args,
                stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd, env=self._env(),
                **_popen_process_group_kwargs(),
            )
        except BaseException:
            self._cleanup_temp_files(scope)
            raise
        assert protocol is not None
        if stdin_data:
            stdin_transport = transport.get_pipe_transport(0)
            try:
                if stdin_transport is None:
                    raise BrokenPipeError("subprocess stdin transport unavailable")
                stdin_transport.write(stdin_data.encode("utf-8"))
                await asyncio.wait_for(
                    protocol.stdin_writable.wait(), timeout=self.timeout)
            except BaseException:
                _terminate_process_tree(transport, force_group=True)
                await protocol.close()
                self._cleanup_temp_files(scope)
                raise
            finally:
                if stdin_transport is not None:
                    stdin_transport.close()
        try:
            while True:
                if _cancel_requested(cancel_event):
                    _terminate_process_tree(transport)
                    raise _cancelled_error(self.name)
                deadline = (self.stream_timeout if protocol.produced
                            else self.first_output_timeout)
                try:
                    kind, payload = await asyncio.wait_for(
                        protocol.queue.get(), timeout=min(deadline, 0.1))
                except asyncio.TimeoutError:
                    if _cancel_requested(cancel_event):
                        _terminate_process_tree(transport)
                        raise _cancelled_error(self.name)
                    # process_exited() owns a separate absolute pipe grace and
                    # will enqueue EOF at its cutoff. Do not misclassify that
                    # short drain window as an idle timeout.
                    if protocol.process_exited_event.is_set():
                        continue
                    if time.monotonic() - protocol.last_output <= deadline:
                        continue
                    # Queue empty for `deadline` with the child alive → the CHILD
                    # is silent (the consumer only ever fills the queue, never
                    # drains it) — a wedged process, e.g. claude blocked on the
                    # Keychain under launchd. Kill it and surface a hang error.
                    _terminate_process_tree(transport)
                    err = self._hang_error(before_output=not protocol.produced)
                    _usage_tracker.record(
                        self.name, model or self.model,
                        latency_ms=int((time.time() - t0) * 1000),
                        prompt_preview=prompt, error_kind=err.kind,
                    )
                    raise err
                if kind == "eof":
                    break
                if _cancel_requested(cancel_event):
                    _terminate_process_tree(transport)
                    raise _cancelled_error(self.name)
                if kind == "limit":
                    raise self._output_limit_error(str(payload or "output"))
                if kind == "reader_error":
                    raise self._pipe_reader_error("async pipe", payload)
                raw = payload
                protocol.consume_line(len(raw))
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for msg in self._stream_normalize(obj, stream_state):
                    if msg.kind == "usage" and msg.usage:
                        final_usage = msg.usage
                    if (msg.kind == "session" and msg.session_id
                            and not session_checked):
                        final_session = msg.session_id
                        _check_session_match(self.name, session_id, msg.session_id)
                        session_checked = True
                    yield msg
            if not protocol.process_exited_event.is_set():
                try:
                    await asyncio.wait_for(
                        protocol.process_exited_event.wait(),
                        timeout=self.stream_timeout,
                    )
                except asyncio.TimeoutError:
                    _terminate_process_tree(transport)
                    raise self._hang_error(before_output=False)
            if protocol.read_error is not None:
                raise self._pipe_reader_error("async pipe", protocol.read_error)
            if protocol.overflow_reason:
                raise self._output_limit_error(protocol.overflow_reason)
            if protocol.returncode != 0:
                err_bytes = b"".join(protocol.stderr_chunks)
                err = classify(
                    self.name, err_bytes.decode("utf-8", "replace"), "",
                    protocol.returncode,
                )
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
            _terminate_process_tree(transport, force_group=True)
            await protocol.close()
            self._cleanup_temp_files(scope)

    def _stream_once(
        self,
        args: list[str],
        *,
        fallback: bool,
        stream_state: object,
        stdin_data: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Iterator[Message]:
        """Run subprocess once, yield normalized messages, raise on failure."""
        if _cancel_requested(cancel_event):
            raise _cancelled_error(self.name)
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE if stdin_data else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=self.cwd, env=self._env(fallback_api_key=fallback),
            **_popen_process_group_kwargs(),
        )
        if stdin_data and proc.stdin:
            try:
                proc.stdin.write(stdin_data.encode("utf-8"))
                proc.stdin.flush()
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
        assert proc.stdout is not None
        # Drain stderr concurrently: if the child writes more than the OS pipe
        # buffer (~64 KB) to stderr while still streaming stdout, an undrained
        # stderr pipe blocks the child, which stalls our stdout loop forever.
        _stderr_chunks: list[bytes] = []
        _stderr_overflow = threading.Event()
        _stderr_sources: list[str] = []
        _stderr_failures: list[BaseException] = []
        _stderr_stop = threading.Event()
        _stderr_thread = threading.Thread(
            target=_drain_binary_into,
            kwargs={
                "pipe": proc.stderr,
                "sink": _stderr_chunks,
                "max_bytes": self.max_stderr_bytes,
                "overflow": _stderr_overflow,
                "source": _stderr_sources,
                "source_name": "stderr",
                "terminate": lambda: _terminate_process_tree(proc),
                "stop": _stderr_stop,
                "process_exited": lambda: proc.poll() is not None,
                "failure": _stderr_failures,
            },
            daemon=True,
        )
        _stderr_thread.start()
        # Read stdout on a background thread with an output watchdog. This kills
        # a child that produces no first line within first_output_timeout, or
        # goes idle past stream_timeout — WITHOUT killing a healthy child just
        # because the consumer below is slow to pull (see _StreamReader).
        reader = _StreamReader(
            proc,
            first_output=self.first_output_timeout,
            idle=self.stream_timeout,
            max_buffer_bytes=self.max_stream_buffer_bytes,
            max_output_bytes=self.max_output_bytes,
            max_events=self.max_stream_events,
            max_line_bytes=self.max_stream_line_bytes,
            terminate=lambda: _terminate_process_tree(proc),
            cancel_event=cancel_event,
        ).start()
        produced_any = False
        loop_done = False
        try:
            for line in reader:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for msg in self._stream_normalize(obj, stream_state):
                    produced_any = True
                    yield msg
            loop_done = True
        finally:
            reader.close()
            # Aborted mid-stream (generator .close()/error): don't wait on a
            # possibly long-running child — kill it.
            if not loop_done:
                _terminate_process_tree(proc, force_group=True)
            wait_error = None
            try:
                proc.wait(timeout=self.stream_timeout)
            except subprocess.TimeoutExpired:
                _terminate_process_tree(proc)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                wait_error = self._hang_error(before_output=False)
            # The leader may have exited before an inherited-child process.
            # Clean the wrapper-owned group after every completed invocation.
            _terminate_process_tree(proc, force_group=True)
            reader.wait_closed(timeout=0.2)
            _stderr_thread.join(timeout=0.2)
            _stderr_stop.set()
            reader.wait_closed(timeout=1)
            if _stderr_thread.is_alive():
                _stderr_thread.join(timeout=1)
            live_pipes = []
            if not reader.is_closed():
                live_pipes.append(proc.stdout)
            if _stderr_thread.is_alive():
                live_pipes.append(proc.stderr)
            _close_popen_pipes(proc, skip=tuple(live_pipes))
            stderr_text = b"".join(_stderr_chunks).decode("utf-8", "replace")
            if wait_error is not None:
                raise wait_error

        if reader.overflow_reason:
            raise self._output_limit_error(reader.overflow_reason)
        if reader.cancelled or _cancel_requested(cancel_event):
            raise _cancelled_error(self.name)
        if reader.read_error is not None:
            raise self._pipe_reader_error("stdout", reader.read_error)
        if _stderr_failures:
            raise self._pipe_reader_error("stderr", _stderr_failures[0])
        if _stderr_overflow.is_set():
            raise self._output_limit_error("stderr")
        if reader.fired:
            # The watchdog killed a wedged child — report the hang, not the
            # SIGKILL exit code (which classify() would mislabel as internal).
            raise self._hang_error(before_output=reader.fired_before_output)

        if proc.returncode not in (0, None):
            err = classify(self.name, stderr_text, "", proc.returncode)
            # attach a marker so the outer retry loop can decide
            err._produced_any = produced_any  # type: ignore[attr-defined]
            raise err

    def _stream_run(
        self,
        args: list[str],
        stdin_data: Optional[str] = None,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> Iterator[Message]:
        """Sync streaming with one auth-fallback retry on pre-stream failure."""
        try:
            once_kwargs = {
                "fallback": False,
                "stream_state": self._new_stream_state(),
                "stdin_data": stdin_data,
            }
            if cancel_event is not None:
                once_kwargs["cancel_event"] = cancel_event
            yield from self._stream_once(args, **once_kwargs)
            return
        except UnifiedError as err:
            produced = getattr(err, "_produced_any", False)
            if (err.kind == "auth_expired"
                    and not produced
                    and self.allow_api_key_fallback
                    and self.api_key_env in os.environ):
                once_kwargs = {
                    "fallback": True,
                    "stream_state": self._new_stream_state(),
                    "stdin_data": stdin_data,
                }
                if cancel_event is not None:
                    once_kwargs["cancel_event"] = cancel_event
                yield from self._stream_once(args, **once_kwargs)
                return
            raise
