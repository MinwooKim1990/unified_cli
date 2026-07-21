"""Bounded bidirectional JSON-lines subprocess transport."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import select
import selectors
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
    DirectoryPin,
    ExecutableIdentity,
    IsolatedEnvironment,
    TransportLimits,
    _require_executable_identity_argv,
    redact_diagnostics,
    strict_json_loads,
    validated_workspace,
    validate_positive_timeout,
)
from ..normalization.events import freeze_json
from .process import (
    _observe_process_returncode_nonreaping,
    _require_nonreaping_process_observation,
    _wait_for_process_exit_nonreaping,
)


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
    entire dedicated process group. A descendant that deliberately creates a
    new session can escape that group; the transport still stops waiting on its
    inherited pipes within a bounded grace period, but killing such a process
    requires an outer OS containment boundary.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        executable_identity: ExecutableIdentity,
        timeout: float = 30.0,
        cwd: Optional[str] = None,
        provider_env: Optional[Mapping[str, str]] = None,
        allowed_provider_env: Sequence[str] = (),
        limits: TransportLimits = TransportLimits(),
        cancellation: Optional[CancellationToken] = None,
        persistent_home: Optional[str] = None,
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
        _require_nonreaping_process_observation()
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
            self.cwd = validated_workspace(clean_cwd)
        if type(limits) is not TransportLimits:
            raise ConfigurationError("limits must be TransportLimits")
        self.limits = limits
        if cancellation is not None and type(cancellation) is not CancellationToken:
            raise ConfigurationError("cancellation must be CancellationToken")
        self.cancellation = cancellation if cancellation is not None else CancellationToken()
        if executable_identity is not None and type(executable_identity) is not ExecutableIdentity:
            raise ConfigurationError("executable_identity must be ExecutableIdentity")
        _require_executable_identity_argv(self.argv[0], executable_identity)
        self._executable_identity = executable_identity
        self._environment = IsolatedEnvironment(
            provider_env,
            allowed_provider_keys=allowed_provider_env,
            persistent_home=persistent_home,
        )
        self._secret_values = self._environment.secret_values
        self._proc: Optional[subprocess.Popen] = None
        self._cwd_pin: Optional[DirectoryPin] = None
        self._environment_active = False
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._stderr = bytearray()
        self._failure: Optional[Exception] = None
        self._failure_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._lifecycle_changed = threading.Condition(self._lifecycle_lock)
        self._lifecycle_state = "new"
        self._close_requested = False
        self._reader_stop = threading.Event()
        self._stderr_done = threading.Event()
        self._threads = []
        self._started = 0.0
        self._closed = False
        self._saw_eof = False

    @property
    def pid(self) -> Optional[int]:
        with self._lifecycle_lock:
            return self._proc.pid if self._proc is not None else None

    @property
    def diagnostics(self) -> str:
        text = bytes(self._stderr).decode("utf-8", "replace")
        return redact_diagnostics(text, self._secret_values)

    def _check_start_checkpoint(self) -> None:
        with self._lifecycle_lock:
            if self._close_requested or self._lifecycle_state != "starting":
                raise TransportError("transport is closed")
        self.cancellation.raise_if_cancelled()

    def _resources_complete(self) -> bool:
        return (
            self._proc is None
            and not self._threads
            and self._cwd_pin is None
            and not self._environment.has_resources
        )

    def start(self) -> "JsonlProcess":
        with self._lifecycle_changed:
            while self._lifecycle_state in ("starting", "closing"):
                self._lifecycle_changed.wait()
            if self._lifecycle_state == "running":
                return self
            if self._lifecycle_state in ("closed", "cleanup_failed"):
                raise TransportError("transport is closed")
            if self._lifecycle_state != "new":
                raise TransportError("transport lifecycle state is invalid")
            self._lifecycle_state = "starting"
            self._close_requested = False
        try:
            # The first checkpoint belongs to the protected lifecycle path.
            # Every failure after ``new -> starting`` must terminalize state and
            # notify close waiters, including a token cancelled before start.
            # Check again at the last safe point before Popen so cancellation
            # during environment construction still cannot spawn.
            self._check_start_checkpoint()
            # Ownership begins before __enter__: a partial enter may have pins
            # or a temporary tree even though it never became active.
            self._environment_active = True
            environment = self._environment.__enter__()
            self._environment_active = self._environment.has_resources
            self._check_start_checkpoint()
            if self.cwd is not None:
                self._cwd_pin = DirectoryPin(self.cwd)
            self._check_start_checkpoint()
            _require_executable_identity_argv(
                self.argv[0], self._executable_identity
            )
            assert self._executable_identity is not None
            self._executable_identity.verify()
            if self._cwd_pin is not None:
                self._cwd_pin.verify()
            environment.verify_for_spawn()
            self._check_start_checkpoint()
            spawned = subprocess.Popen(
                list(self.argv),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self._cwd_pin if self._cwd_pin is not None else None,
                env=environment.env,
                shell=False,
                start_new_session=True,
                close_fds=True,
            )
            with self._lifecycle_lock:
                self._proc = spawned
            # A close request or cancellation that raced with Popen owns this
            # just-created process and is observed before any success return.
            self._check_start_checkpoint()
            if self._cwd_pin is not None:
                self._cwd_pin.verify()
            environment.verify_after_spawn()
            self._executable_identity.verify_metadata()
            self._check_start_checkpoint()
            self._started = time.monotonic()
            stdout_thread = threading.Thread(
                target=self._read_stdout,
                name="unified-cli-jsonl-{}-stdout".format(self._proc.pid),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=self._read_stderr,
                name="unified-cli-jsonl-{}-stderr".format(self._proc.pid),
                daemon=True,
            )
            self._threads = [stdout_thread, stderr_thread]
            for thread in self._threads:
                thread.start()
            self._check_start_checkpoint()
        except BaseException as failure:
            with self._lifecycle_changed:
                self._lifecycle_state = "closing"
            cleanup_failure = None
            try:
                cleanup_failure = self._cleanup_owned_resources(abort=True)
            except BaseException as caught:
                cleanup_failure = caught
            with self._lifecycle_changed:
                complete = self._resources_complete()
                self._environment_active = self._environment.has_resources
                self._closed = complete
                self._lifecycle_state = "closed" if complete else "cleanup_failed"
                self._lifecycle_changed.notify_all()
            if cleanup_failure is not None and isinstance(
                cleanup_failure, TransportError
            ) and "reaped" in str(cleanup_failure):
                raise cleanup_failure from failure
            if isinstance(failure, (OSError, UnicodeError)):
                raise TransportError("failed to start extension subprocess") from None
            raise
        with self._lifecycle_changed:
            self._lifecycle_state = "running"
            self._lifecycle_changed.notify_all()
        return self

    def __enter__(self) -> "JsonlProcess":
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self.close(abort=exc_type is not None or not self._saw_eof)
        except BaseException as failure:
            if exc_type is None or (
                isinstance(failure, TransportError) and "reaped" in str(failure)
            ):
                raise

    def _set_failure(self, failure: Exception) -> None:
        with self._failure_lock:
            if self._failure is None:
                self._failure = failure
        self._kill_group(signal.SIGKILL)

    def _record_worker_escape(self, message: str, caught: BaseException) -> None:
        if self._reader_stop.is_set():
            return
        failure = TransportError(message)
        failure.__cause__ = caught
        self._set_failure(failure)

    def _raise_and_close(self, failure: Exception, *, store: bool = True) -> None:
        if store:
            self._set_failure(failure)
        self.close()
        raise failure

    def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        stream = proc.stdout
        selector = None
        pending = bytearray()
        total = 0
        count = 0
        ended = False
        leader_exited_at = None
        try:
            descriptor = stream.fileno()
            os.set_blocking(descriptor, False)
            selector = selectors.DefaultSelector()
            selector.register(descriptor, selectors.EVENT_READ)
            while not self._reader_stop.is_set():
                now = time.monotonic()
                if _observe_process_returncode_nonreaping(proc) is not None:
                    if leader_exited_at is None:
                        leader_exited_at = now
                    elif now - leader_exited_at >= 0.2:
                        ended = True
                        break
                events = selector.select(timeout=0.02)
                if not events:
                    continue
                try:
                    chunk = os.read(descriptor, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    ended = True
                    break
                pending.extend(chunk)
                while True:
                    newline = pending.find(b"\n")
                    if newline < 0:
                        break
                    line_length = newline + 1
                    if line_length > self.limits.max_line_bytes:
                        self._set_failure(
                            LimitExceeded("JSONL line exceeds configured limit")
                        )
                        return
                    line = bytes(pending[:line_length])
                    del pending[:line_length]
                    total += line_length
                    count += 1
                    if total > self.limits.max_output_bytes:
                        self._set_failure(
                            LimitExceeded("JSONL output exceeds configured limit")
                        )
                        return
                    if count > self.limits.max_events:
                        self._set_failure(
                            LimitExceeded("JSONL event count exceeds configured limit")
                        )
                        return
                    self._queue.put(line)
                if len(pending) >= self.limits.max_line_bytes:
                    self._set_failure(
                        LimitExceeded("JSONL line exceeds configured limit")
                    )
                    return
            if ended and pending:
                self._set_failure(
                    ProtocolError("JSONL protocol message is not LF-terminated")
                )
        except BaseException as caught:
            self._record_worker_escape(
                "failed to read subprocess protocol output", caught
            )
        finally:
            if selector is not None:
                try:
                    selector.close()
                except BaseException as caught:
                    self._record_worker_escape(
                        "failed to close subprocess protocol reader", caught
                    )
            try:
                stream.close()
            except BaseException as caught:
                self._record_worker_escape(
                    "failed to close subprocess protocol output", caught
                )
            self._queue.put(_EOF)

    def _read_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        stream = proc.stderr
        selector = None
        leader_exited_at = None
        try:
            descriptor = stream.fileno()
            os.set_blocking(descriptor, False)
            selector = selectors.DefaultSelector()
            selector.register(descriptor, selectors.EVENT_READ)
            while not self._reader_stop.is_set():
                now = time.monotonic()
                if _observe_process_returncode_nonreaping(proc) is not None:
                    if leader_exited_at is None:
                        leader_exited_at = now
                    elif now - leader_exited_at >= 0.2:
                        return
                events = selector.select(timeout=0.02)
                if not events:
                    continue
                try:
                    chunk = os.read(descriptor, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    return
                remaining = self.limits.max_stderr_bytes - len(self._stderr)
                if len(chunk) > remaining:
                    if remaining:
                        self._stderr.extend(chunk[:remaining])
                    self._set_failure(LimitExceeded("stderr exceeds configured limit"))
                    return
                self._stderr.extend(chunk)
        except BaseException as caught:
            self._record_worker_escape("failed to read subprocess stderr", caught)
        finally:
            if selector is not None:
                try:
                    selector.close()
                except BaseException as caught:
                    self._record_worker_escape(
                        "failed to close subprocess stderr reader", caught
                    )
            try:
                stream.close()
            except BaseException as caught:
                self._record_worker_escape(
                    "failed to close subprocess stderr", caught
                )
            self._stderr_done.set()

    def _kill_group(
        self, sig: signal.Signals, proc: Optional[subprocess.Popen] = None
    ) -> None:
        process = self._proc if proc is None else proc
        if process is None:
            return
        with self._lifecycle_lock:
            if process.returncode is not None:
                return
            try:
                os.killpg(process.pid, sig)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass
            if process.returncode is not None:
                return
            try:
                os.kill(process.pid, sig)
            except (ProcessLookupError, OSError):
                pass

    @staticmethod
    def _wait_bounded(proc: subprocess.Popen, timeout: float) -> Optional[int]:
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def _running_process(self) -> subprocess.Popen:
        """Return a stable process reference or report that close has won."""

        with self._lifecycle_lock:
            proc = self._proc
            if self._lifecycle_state != "running" or proc is None:
                raise TransportError("transport is closed")
            return proc

    def _terminate_and_reap(self, proc: subprocess.Popen) -> int:
        """Escalate TERM/KILL and return only after reaping the leader."""

        with self._lifecycle_lock:
            if proc.returncode is not None:
                return proc.returncode
            self._kill_group(signal.SIGTERM, proc)
            observation_failed = False
            try:
                observed_returncode = _wait_for_process_exit_nonreaping(proc, 0.2)
            except BaseException:
                observation_failed = True
                observed_returncode = None
            # The leader remains waitable, retaining the PID/PGID identity,
            # until descendants have received the final group signal.
            self._kill_group(signal.SIGKILL, proc)
            if observed_returncode is None and not observation_failed:
                try:
                    _wait_for_process_exit_nonreaping(proc, 0.8)
                except BaseException:
                    pass
            returncode = self._wait_bounded(proc, 0.2)
            if returncode is None:
                raise TransportError(
                    "extension subprocess could not be reaped after termination"
                )
            return returncode

    def send(self, value: Any) -> None:
        try:
            bounded = _plain_json(freeze_json(value, drop_reasoning=False))
            payload = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            data = (payload + "\n").encode("utf-8")
        except (TypeError, ValueError, UnicodeError, ProtocolError):
            raise ProtocolError("outbound value is not bounded JSON") from None
        if len(data) > self.limits.max_line_bytes:
            raise LimitExceeded("outbound JSONL line exceeds configured limit")
        try:
            self.start()
            proc = self._running_process()
            with self._write_lock:
                self._write_with_deadline(proc, data)
        except BaseException as failure:
            # A failed write makes framing recovery impossible. Own complete
            # teardown here so a direct ``send()`` caller cannot strand the
            # provider process or its descendants.
            if self._proc is not None:
                try:
                    self.close()
                except BaseException as cleanup_failure:
                    if isinstance(cleanup_failure, TransportError) and "reaped" in str(
                        cleanup_failure
                    ):
                        raise cleanup_failure from failure
            raise

    def _write_with_deadline(self, proc: subprocess.Popen, data: bytes) -> None:
        """Write a complete frame without ever blocking past the operation deadline."""

        stdin = proc.stdin
        if stdin is None:
            raise TransportError("subprocess protocol input closed")
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
        self.start()
        if self._proc is not None and self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except OSError:
                pass

    def _synchronize_stderr_after_exit(self, remaining: float) -> Optional[Exception]:
        """Boundedly finish stderr for every exit before classifying status."""

        drain_timeout = min(0.35, max(0.0, remaining))
        if not self._stderr_done.wait(timeout=drain_timeout):
            if remaining <= drain_timeout:
                return TransportTimeout("subprocess did not exit before its deadline")
            return TransportError("subprocess stderr reader did not finish bounded drain")
        with self._failure_lock:
            return self._failure

    def receive(self) -> Optional[Any]:
        self.start()
        proc = self._running_process()
        while True:
            with self._failure_lock:
                failure = self._failure
            if failure is not None:
                self._raise_and_close(failure, store=False)
            if self.cancellation.cancelled:
                failure = TransportCancelled("extension operation cancelled")
                self._raise_and_close(failure)
            remaining = self.timeout - (time.monotonic() - self._started)
            if remaining <= 0:
                failure = TransportTimeout("extension subprocess timed out")
                self._raise_and_close(failure)
            try:
                item = self._queue.get(timeout=max(0.001, min(0.05, max(remaining, 0.001))))
            except queue.Empty:
                with self._failure_lock:
                    failure = self._failure
                if failure is not None:
                    self._raise_and_close(failure, store=False)
                continue
            with self._failure_lock:
                failure = self._failure
            if failure is not None:
                self._raise_and_close(failure, store=False)
            if item is _EOF:
                self._saw_eof = True
                with self._failure_lock:
                    failure = self._failure
                if failure is not None:
                    self._raise_and_close(failure, store=False)
                try:
                    remaining = self.timeout - (time.monotonic() - self._started)
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(self.argv, self.timeout)
                    returncode = _wait_for_process_exit_nonreaping(
                        proc, min(1.0, remaining)
                    )
                    if returncode is None:
                        raise subprocess.TimeoutExpired(self.argv, self.timeout)
                except subprocess.TimeoutExpired:
                    failure = TransportTimeout(
                        "subprocess did not exit before its deadline"
                    )
                    self._raise_and_close(failure)
                remaining = self.timeout - (time.monotonic() - self._started)
                failure = self._synchronize_stderr_after_exit(remaining)
                if failure is not None:
                    # Reader/resource failures have precedence over an exit
                    # status because diagnostics may be incomplete or unsafe.
                    self._raise_and_close(failure, store=False)
                with self._failure_lock:
                    failure = self._failure
                if failure is not None:
                    self._raise_and_close(failure, store=False)
                if returncode != 0:
                    failure = ProcessFailed(returncode, self.diagnostics)
                    self._raise_and_close(failure)
                self.close(abort=False)
                return None
            try:
                decoded = item.decode("utf-8")
            except UnicodeDecodeError:
                failure = ProtocolError("JSONL output is not valid UTF-8")
                self._raise_and_close(failure)
            try:
                value = strict_json_loads(decoded)
            except (ValueError, RecursionError):
                failure = ProtocolError("malformed JSONL protocol message")
                self._raise_and_close(failure)
            if not isinstance(value, dict):
                failure = ProtocolError("JSONL protocol message must be an object")
                self._raise_and_close(failure)
            try:
                bounded = _plain_json(freeze_json(value, drop_reasoning=False))
            except ProtocolError:
                failure = ProtocolError(
                    "JSONL protocol message is outside JSON bounds"
                )
                self._raise_and_close(failure)
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
            self._raise_and_close(failure)
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
                    break
                yield value
        except BaseException:
            try:
                self.close(abort=not self._saw_eof)
            except BaseException as cleanup_failure:
                if isinstance(cleanup_failure, TransportError) and "reaped" in str(
                    cleanup_failure
                ):
                    raise
            raise
        else:
            self.close(abort=not self._saw_eof)

    async def aiter_messages(self) -> AsyncIterator[Dict[str, Any]]:
        try:
            while True:
                value = await self.receive_async()
                if value is None:
                    break
                yield value
        except BaseException:
            try:
                await self.close_async(abort=not self._saw_eof)
            except BaseException as cleanup_failure:
                if isinstance(cleanup_failure, TransportError) and "reaped" in str(
                    cleanup_failure
                ):
                    raise
            raise
        else:
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

    def _cleanup_owned_resources(self, *, abort: bool) -> Optional[BaseException]:
        """Attempt one complete cleanup pass while retaining failed owners."""

        if not abort and self._saw_eof and self._proc is not None:
            try:
                returncode = _observe_process_returncode_nonreaping(self._proc)
            except BaseException:
                returncode = None
            if returncode is not None and not self._stderr_done.is_set():
                self._stderr_done.wait(timeout=0.35)
        self._reader_stop.set()
        proc = self._proc
        threads = list(self._threads)
        cleanup_failure = None
        reaped = proc is None or proc.returncode is not None
        streams_closed = proc is None
        try:
            if proc is not None:
                try:
                    self._terminate_and_reap(proc)
                    reaped = True
                except BaseException as failure:
                    if isinstance(failure, TransportError):
                        cleanup_failure = failure
                    else:
                        cleanup_failure = TransportError(
                            "extension subprocess could not be reaped after termination"
                        )
                        cleanup_failure.__cause__ = failure
                streams_closed = True
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is None:
                        continue
                    try:
                        already_closed = stream.closed
                    except BaseException as failure:
                        streams_closed = False
                        if cleanup_failure is None:
                            cleanup_failure = failure
                        continue
                    if already_closed:
                        continue
                    try:
                        stream.close()
                    except BaseException as failure:
                        if cleanup_failure is None:
                            cleanup_failure = failure
                    try:
                        confirmed_closed = stream.closed
                    except BaseException as failure:
                        confirmed_closed = False
                        if cleanup_failure is None:
                            cleanup_failure = failure
                    if not confirmed_closed:
                        streams_closed = False
                        if cleanup_failure is None:
                            cleanup_failure = TransportError(
                                "extension subprocess pipe did not close"
                            )
            join_deadline = time.monotonic() + 0.5
            alive_threads = []
            for thread in threads:
                if thread.ident is None:
                    continue
                try:
                    thread.join(timeout=max(0.0, join_deadline - time.monotonic()))
                except BaseException as failure:
                    if cleanup_failure is None:
                        cleanup_failure = failure
                try:
                    if thread.is_alive():
                        alive_threads.append(thread)
                except BaseException as failure:
                    alive_threads.append(thread)
                    if cleanup_failure is None:
                        cleanup_failure = failure
            self._threads = alive_threads
            if alive_threads and cleanup_failure is None:
                cleanup_failure = TransportError(
                    "extension subprocess reader did not stop"
                )
        finally:
            pin = self._cwd_pin
            if pin is not None:
                try:
                    pin.close()
                except BaseException as failure:
                    if cleanup_failure is None:
                        cleanup_failure = failure
                if pin.closed:
                    self._cwd_pin = None
            if self._environment_active or self._environment.has_resources:
                try:
                    self._environment.__exit__(None, None, None)
                except BaseException as failure:
                    if cleanup_failure is None:
                        cleanup_failure = failure
                self._environment_active = self._environment.has_resources
        if reaped and streams_closed and not self._threads:
            self._proc = None
        return cleanup_failure

    def close(self, *, abort: bool = True) -> None:
        with self._lifecycle_changed:
            if self._lifecycle_state == "starting":
                self._close_requested = True
                while self._lifecycle_state in ("starting", "closing"):
                    self._lifecycle_changed.wait()
            while self._lifecycle_state == "closing":
                self._lifecycle_changed.wait()
            if self._lifecycle_state == "closed":
                return
            if self._lifecycle_state not in ("new", "running", "cleanup_failed"):
                raise TransportError("transport lifecycle state is invalid")
            self._close_requested = True
            self._lifecycle_state = "closing"

        cleanup_failure = None
        try:
            cleanup_failure = self._cleanup_owned_resources(abort=abort)
        except BaseException as caught:
            cleanup_failure = caught
        with self._lifecycle_changed:
            complete = self._resources_complete()
            self._environment_active = self._environment.has_resources
            self._closed = complete
            self._lifecycle_state = "closed" if complete else "cleanup_failed"
            self._lifecycle_changed.notify_all()
        if cleanup_failure is not None:
            raise cleanup_failure


__all__ = ["JsonlProcess"]
