"""Bounded bidirectional JSON-lines subprocess transport."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import select
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any, AsyncIterator, Dict, Iterator, Optional

from ..errors import (
    ConfigurationError,
    LimitExceeded,
    ProcessFailed,
    ProtocolError,
    TransportCancelled,
    TransportError,
    TransportTimeout,
    UnsupportedPlatformError,
)
from .security import (
    CancellationToken,
    IsolatedEnvironment,
    TransportLimits,
    redact_diagnostics,
    strict_json_loads,
    validate_positive_timeout,
)
from ..normalization.events import freeze_json


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


_EOF = object()


class JsonlProcess:
    """A POSIX process-group-isolated JSONL peer.

    Stdout is the sole protocol channel. Stderr is drained concurrently into a
    bounded, redacted diagnostic buffer. Closing an iterator early, timeout,
    explicit cancellation, malformed input, or a limit violation tears down the
    entire dedicated process group.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 30.0,
        cwd: Optional[str] = None,
        provider_env: Optional[Mapping[str, str]] = None,
        allowed_provider_env: Sequence[str] = (),
        limits: TransportLimits = TransportLimits(),
        cancellation: Optional[CancellationToken] = None,
    ) -> None:
        if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
            raise ConfigurationError("subprocess argv must be a nonempty string sequence")
        clean_argv = []
        try:
            iterator = iter(argv)
            for index, item in enumerate(iterator):
                if index >= 1024:
                    raise ConfigurationError("subprocess argv exceeds 1024 items")
                if type(item) is not str or not item or "\x00" in item:
                    raise ConfigurationError("subprocess argv contains an invalid item")
                clean_argv.append(item)
        except ConfigurationError:
            raise
        except Exception:
            raise ConfigurationError("subprocess argv is malformed") from None
        if not clean_argv:
            raise ConfigurationError("subprocess argv must be nonempty")
        if os.name != "posix":
            raise UnsupportedPlatformError(
                "subprocess transports require POSIX process-group cleanup"
            )
        self.argv = tuple(clean_argv)
        self.timeout = validate_positive_timeout(timeout)
        if cwd is None:
            self.cwd = None
        else:
            try:
                clean_cwd = os.fspath(cwd)
                encoded_cwd = clean_cwd.encode("utf-8", "strict") if type(clean_cwd) is str else b""
            except (TypeError, UnicodeError):
                raise ConfigurationError("subprocess cwd must be a filesystem path") from None
            if type(clean_cwd) is not str or "\x00" in clean_cwd or len(encoded_cwd) > 16 * 1024:
                raise ConfigurationError("subprocess cwd is invalid")
            self.cwd = clean_cwd
        if type(limits) is not TransportLimits:
            raise ConfigurationError("limits must be TransportLimits")
        self.limits = limits
        if cancellation is not None and type(cancellation) is not CancellationToken:
            raise ConfigurationError("cancellation must be CancellationToken")
        self.cancellation = cancellation if cancellation is not None else CancellationToken()
        self._environment = IsolatedEnvironment(
            provider_env,
            allowed_provider_keys=allowed_provider_env,
        )
        self._secret_values = self._environment.secret_values
        self._proc: Optional[subprocess.Popen] = None
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._stderr = bytearray()
        self._failure: Optional[Exception] = None
        self._failure_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._threads = []
        self._started = 0.0
        self._closed = False
        self._saw_eof = False

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc is not None else None

    @property
    def diagnostics(self) -> str:
        text = bytes(self._stderr).decode("utf-8", "replace")
        return redact_diagnostics(text, self._secret_values)

    def start(self) -> "JsonlProcess":
        if self._proc is not None:
            return self
        if self._closed:
            raise TransportError("transport is closed")
        environment = self._environment.__enter__()
        try:
            self._proc = subprocess.Popen(
                list(self.argv),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.cwd,
                env=environment.env,
                shell=False,
                start_new_session=True,
                close_fds=True,
            )
        except (OSError, UnicodeError) as exc:
            self._environment.__exit__(None, None, None)
            raise TransportError("failed to start extension subprocess") from None
        self._started = time.monotonic()
        stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._threads = [stdout_thread, stderr_thread]
        for thread in self._threads:
            thread.start()
        return self

    def __enter__(self) -> "JsonlProcess":
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close(abort=exc_type is not None or not self._saw_eof)

    def _set_failure(self, failure: Exception) -> None:
        with self._failure_lock:
            if self._failure is None:
                self._failure = failure
        self._kill_group(signal.SIGKILL)

    def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        total = 0
        count = 0
        try:
            while True:
                line = self._proc.stdout.readline(self.limits.max_line_bytes + 1)
                if not line:
                    break
                if len(line) > self.limits.max_line_bytes or (
                    len(line) == self.limits.max_line_bytes and not line.endswith(b"\n")
                ):
                    self._set_failure(LimitExceeded("JSONL line exceeds configured limit"))
                    break
                if not line.endswith(b"\n"):
                    self._set_failure(
                        ProtocolError("JSONL protocol message is not LF-terminated")
                    )
                    break
                total += len(line)
                count += 1
                if total > self.limits.max_output_bytes:
                    self._set_failure(LimitExceeded("JSONL output exceeds configured limit"))
                    break
                if count > self.limits.max_events:
                    self._set_failure(LimitExceeded("JSONL event count exceeds configured limit"))
                    break
                self._queue.put(line)
        except Exception:
            self._set_failure(TransportError("failed to read subprocess protocol output"))
        finally:
            self._queue.put(_EOF)

    def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                remaining = self.limits.max_stderr_bytes - len(self._stderr)
                chunk = self._proc.stderr.read(min(65536, remaining + 1))
                if not chunk:
                    return
                if len(chunk) > remaining:
                    if remaining:
                        self._stderr.extend(chunk[:remaining])
                    self._set_failure(LimitExceeded("stderr exceeds configured limit"))
                    return
                self._stderr.extend(chunk)
        except Exception:
            return

    def _kill_group(self, sig: signal.Signals) -> None:
        if self._proc is None:
            return
        try:
            os.killpg(self._proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                if self._proc.poll() is None:
                    self._proc.kill()
            except (ProcessLookupError, OSError):
                pass

    def send(self, value: Any) -> None:
        self.start()
        try:
            bounded = _plain_json(freeze_json(value, drop_reasoning=False))
            payload = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, UnicodeError, ProtocolError):
            raise ProtocolError("outbound value is not bounded JSON") from None
        data = (payload + "\n").encode("utf-8")
        if len(data) > self.limits.max_line_bytes:
            raise LimitExceeded("outbound JSONL line exceeds configured limit")
        assert self._proc is not None and self._proc.stdin is not None
        try:
            with self._write_lock:
                self._write_with_deadline(data)
        except (TransportError, ProtocolError):
            # A failed write makes framing recovery impossible. Own complete
            # teardown here so a direct ``send()`` caller cannot strand the
            # provider process or its descendants.
            self.close()
            raise

    def _write_with_deadline(self, data: bytes) -> None:
        """Write a complete frame without ever blocking past the operation deadline."""

        assert self._proc is not None and self._proc.stdin is not None
        stdin = self._proc.stdin
        try:
            descriptor = stdin.fileno()
            os.set_blocking(descriptor, False)
        except (OSError, ValueError):
            failure = TransportError("subprocess protocol input closed")
            self._set_failure(failure)
            raise failure from None

        view = memoryview(data)
        offset = 0
        try:
            while offset < len(view):
                with self._failure_lock:
                    failure = self._failure
                if failure is not None:
                    raise failure
                if self.cancellation.cancelled:
                    failure = TransportCancelled("extension operation cancelled")
                    self._set_failure(failure)
                    raise failure
                remaining = self.remaining_timeout()
                try:
                    _, writable, _ = select.select(
                        [], [descriptor], [], min(0.05, remaining)
                    )
                except InterruptedError:
                    continue
                except (OSError, ValueError):
                    failure = TransportError("subprocess protocol input closed")
                    self._set_failure(failure)
                    raise failure from None
                if not writable:
                    continue
                try:
                    written = os.write(descriptor, view[offset : offset + 65536])
                except BlockingIOError:
                    continue
                except (BrokenPipeError, OSError):
                    failure = TransportError("subprocess protocol input closed")
                    self._set_failure(failure)
                    raise failure from None
                if written <= 0:
                    failure = TransportError("subprocess protocol input closed")
                    self._set_failure(failure)
                    raise failure
                offset += written
        finally:
            try:
                os.set_blocking(descriptor, True)
            except (OSError, ValueError):
                pass

    async def send_async(self, value: Any) -> None:
        loop = asyncio.get_running_loop()
        worker = loop.run_in_executor(None, self.send, value)
        try:
            await worker
        except asyncio.CancelledError:
            self.cancellation.cancel()
            self._kill_group(signal.SIGKILL)
            await self._close_after_async_cancel(loop)
            raise

    def close_stdin(self) -> None:
        if self._proc is not None and self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except OSError:
                pass

    def receive(self) -> Optional[Any]:
        self.start()
        while True:
            with self._failure_lock:
                failure = self._failure
            if failure is not None:
                raise failure
            if self.cancellation.cancelled:
                failure = TransportCancelled("extension operation cancelled")
                self._set_failure(failure)
                raise failure
            remaining = self.timeout - (time.monotonic() - self._started)
            if remaining <= 0:
                failure = TransportTimeout("extension subprocess timed out")
                self._set_failure(failure)
                raise failure
            try:
                item = self._queue.get(timeout=max(0.001, min(0.05, max(remaining, 0.001))))
            except queue.Empty:
                with self._failure_lock:
                    failure = self._failure
                if failure is not None:
                    raise failure
                continue
            with self._failure_lock:
                failure = self._failure
            if failure is not None:
                raise failure
            if item is _EOF:
                self._saw_eof = True
                with self._failure_lock:
                    failure = self._failure
                if failure is not None:
                    raise failure
                assert self._proc is not None
                try:
                    remaining = self.timeout - (time.monotonic() - self._started)
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(self.argv, self.timeout)
                    returncode = self._proc.wait(timeout=min(1.0, remaining))
                except subprocess.TimeoutExpired:
                    self._set_failure(TransportTimeout("subprocess did not exit before its deadline"))
                    raise self._failure from None
                with self._failure_lock:
                    failure = self._failure
                if failure is not None:
                    raise failure
                if returncode != 0:
                    raise ProcessFailed(returncode, self.diagnostics)
                return None
            try:
                decoded = item.decode("utf-8")
            except UnicodeDecodeError:
                self._set_failure(ProtocolError("JSONL output is not valid UTF-8"))
                raise self._failure from None
            try:
                value = strict_json_loads(decoded)
            except (ValueError, RecursionError):
                self._set_failure(ProtocolError("malformed JSONL protocol message"))
                raise self._failure from None
            if not isinstance(value, dict):
                self._set_failure(ProtocolError("JSONL protocol message must be an object"))
                raise self._failure
            try:
                bounded = _plain_json(freeze_json(value, drop_reasoning=False))
            except ProtocolError:
                self._set_failure(ProtocolError("JSONL protocol message is outside JSON bounds"))
                raise self._failure from None
            return bounded

    def reset_timeout(self) -> None:
        """Start a fresh per-operation deadline for a persistent peer."""

        self.start()
        self._started = time.monotonic()

    def remaining_timeout(self) -> float:
        """Return the current operation budget or fail at its deadline."""

        self.start()
        remaining = self.timeout - (time.monotonic() - self._started)
        if remaining <= 0:
            failure = TransportTimeout("extension subprocess timed out")
            self._set_failure(failure)
            raise failure
        return remaining

    async def receive_async(self) -> Optional[Any]:
        loop = asyncio.get_running_loop()
        worker = loop.run_in_executor(None, self.receive)
        try:
            return await worker
        except asyncio.CancelledError:
            self.cancellation.cancel()
            self._kill_group(signal.SIGKILL)
            await self._close_after_async_cancel(loop)
            raise

    def iter_messages(self) -> Iterator[Dict[str, Any]]:
        try:
            while True:
                value = self.receive()
                if value is None:
                    return
                yield value
        finally:
            self.close(abort=not self._saw_eof)

    async def aiter_messages(self) -> AsyncIterator[Dict[str, Any]]:
        try:
            while True:
                value = await self.receive_async()
                if value is None:
                    return
                yield value
        finally:
            await self.close_async(abort=not self._saw_eof)

    async def _close_after_async_cancel(self, loop: asyncio.AbstractEventLoop) -> None:
        cleanup = loop.run_in_executor(None, self.close)
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # A second cancellation must not make cleanup ownership disappear.
            cleanup.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
            raise

    async def close_async(self, *, abort: bool = True) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self.close(abort=abort))

    def close(self, *, abort: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is not None:
            # Always signal the dedicated group: a successful leader may have
            # left descendants behind. This group belongs solely to this call.
            self._kill_group(signal.SIGTERM)
            try:
                proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
            self._kill_group(signal.SIGKILL)
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._kill_group(signal.SIGKILL)
        for thread in self._threads:
            thread.join(timeout=0.5)
        if self._proc is not None:
            self._environment.__exit__(None, None, None)


__all__ = ["JsonlProcess"]
