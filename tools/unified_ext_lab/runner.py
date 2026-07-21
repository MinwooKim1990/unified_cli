"""Bounded, identity-bound subprocess execution for the extension lab.

The runner deliberately has no shell, PATH lookup, ambient environment copy,
or unbounded ``communicate`` call.  A caller injects the small ``Runner``
protocol into higher layers, which makes command construction testable without
starting Docker or a provider executable.
"""

from __future__ import annotations

import errno
import hashlib
import math
import os
import select
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Optional, Protocol, Tuple

from .errors import InvariantRefusalError, RunnerFailureError, UsageStateError


DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_POLL_SECONDS = 0.05
_REAP_TIMEOUT_SECONDS = 1.0
_MAX_TIMEOUT_SECONDS = 3600.0
_CLEANUP_UNCERTAIN = "runner cleanup uncertain"
_OBSERVER_UNAVAILABLE = "runner non-reaping observation unavailable"

_EXIT_RUNNING = "running"
_EXIT_OBSERVED = "observed"
_EXIT_OBSERVER_UNAVAILABLE = "unavailable"
_EXIT_IDENTITY_LOST = "identity-lost"


@dataclass(frozen=True)
class CommandResult:
    """An immutable, already-bounded command result."""

    argv: Tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    def __post_init__(self) -> None:
        if type(self.argv) is not tuple or not self.argv:
            raise UsageStateError("invalid command result argv")
        if any(type(argument) is not str for argument in self.argv):
            raise UsageStateError("invalid command result argv")
        if type(self.returncode) is not int:
            raise UsageStateError("invalid command result return code")
        if type(self.stdout) is not str or type(self.stderr) is not str:
            raise UsageStateError("invalid command result output")


class Runner(Protocol):
    """The complete executor surface accepted by the Docker lifecycle."""

    def run(self, argv: Tuple[str, ...], *, timeout: float) -> CommandResult:
        """Run one exact argv tuple or raise a stable lab error."""


@dataclass(frozen=True)
class ExecutableIdentity:
    """Filesystem and content identity captured for one executable."""

    canonical_path: str
    device: int
    inode: int
    mode: int
    uid: int
    size: int
    mtime_ns: int
    sha256: str


@dataclass
class _CleanupState:
    """Track the one-way signal-before-reap subprocess lifecycle."""

    group_signal_attempted: bool = False
    cleanup_uncertain: bool = False
    identity_lost: bool = False
    leader_exit_observed: bool = False


class _WaitidExitObserver:
    """Observe one child with waitid while deliberately leaving it waitable."""

    def __init__(self, process: subprocess.Popen) -> None:
        self._process = process

    def register(self) -> str:
        return self.observe()

    def observe(self) -> str:
        if self._process.returncode is not None:
            return _EXIT_IDENTITY_LOST
        try:
            result = os.waitid(
                os.P_PID,
                self._process.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except ChildProcessError:
            return _EXIT_IDENTITY_LOST
        except (AttributeError, NotImplementedError):
            return _EXIT_OBSERVER_UNAVAILABLE
        except OSError as error:
            if error.errno in (errno.EINVAL, errno.ENOSYS):
                return _EXIT_OBSERVER_UNAVAILABLE
            raise
        if result is None or getattr(result, "si_pid", 0) == 0:
            return _EXIT_RUNNING
        if result.si_pid != self._process.pid:
            raise RunnerFailureError(_CLEANUP_UNCERTAIN)
        return _EXIT_OBSERVED

    def close(self) -> None:
        return None


class _KqueueExitObserver:
    """Observe process exit on Darwin/BSD without consuming wait status."""

    def __init__(self, process: subprocess.Popen) -> None:
        self._process = process
        self._queue = select.kqueue()

    def register(self) -> str:
        flags = select.KQ_EV_ADD | select.KQ_EV_ENABLE
        if hasattr(select, "KQ_EV_ONESHOT"):
            flags |= select.KQ_EV_ONESHOT
        event = select.kevent(
            self._process.pid,
            filter=select.KQ_FILTER_PROC,
            flags=flags,
            fflags=select.KQ_NOTE_EXIT,
        )
        self._queue.control([event], 0, 0)
        return _EXIT_RUNNING

    def observe(self) -> str:
        if self._process.returncode is not None:
            return _EXIT_IDENTITY_LOST
        try:
            events = self._queue.control(None, 1, 0)
        except InterruptedError:
            return _EXIT_RUNNING
        if not events:
            return _EXIT_RUNNING
        event = events[0]
        if event.ident != self._process.pid:
            raise RunnerFailureError(_CLEANUP_UNCERTAIN)
        return _EXIT_OBSERVED

    def close(self) -> None:
        self._queue.close()


def _digest_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb", buffering=0) as handle:
        while True:
            chunk = handle.read(_READ_CHUNK_BYTES)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _capture_identity(path: object) -> ExecutableIdentity:
    if type(path) is not str or not path or "\x00" in path:
        raise UsageStateError("invalid runner executable")
    if not os.path.isabs(path):
        raise UsageStateError("runner executable must be absolute")
    canonical = os.path.realpath(path)
    if canonical != path or os.path.normpath(path) != path:
        raise InvariantRefusalError("runner executable must be canonical")
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise InvariantRefusalError("runner executable is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise InvariantRefusalError("runner executable must be a regular file")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise InvariantRefusalError("runner executable is not identity-safe")
    if not os.access(path, os.X_OK):
        raise InvariantRefusalError("runner executable is not executable")
    try:
        digest = _digest_file(path)
    except OSError as error:
        raise InvariantRefusalError("runner executable identity is unreadable") from error
    return ExecutableIdentity(
        canonical_path=canonical,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        uid=metadata.st_uid,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        sha256=digest,
    )


def _validate_argv(argv: object, executable: str) -> Tuple[str, ...]:
    if type(argv) is not tuple or not argv:
        raise UsageStateError("runner argv must be a non-empty tuple")
    for argument in argv:
        if type(argument) is not str or "\x00" in argument:
            raise UsageStateError("invalid runner argument")
    if argv[0] != executable:
        raise InvariantRefusalError("runner executable does not match bound identity")
    return argv


class SubprocessRunner:
    """Run a single identity-bound executable in a private environment.

    Cancellation is intentionally runner-owned rather than supplied as an
    arbitrary callback.  ``cancel`` is thread-safe and affects the active and
    all later calls.  Construct another runner for later independent work.
    """

    def __init__(
        self,
        executable: str,
        *,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        if type(max_output_bytes) is not int or max_output_bytes < 1:
            raise UsageStateError("invalid runner output limit")
        self._identity = _capture_identity(executable)
        self._max_output_bytes = max_output_bytes
        self._cancelled = threading.Event()
        self._run_lock = threading.Lock()
        self._closed = False
        created_root = tempfile.mkdtemp(prefix="unified-ext-lab-runner-")
        try:
            self._private_root = os.path.realpath(created_root)
            os.chmod(self._private_root, 0o700)
            self._bound_executable = os.path.join(self._private_root, "executable")
            shutil.copyfile(self._identity.canonical_path, self._bound_executable)
            os.chmod(self._bound_executable, 0o500)
            self._bound_identity = _capture_identity(self._bound_executable)
            self._check_source_identity()
            if self._bound_identity.sha256 != self._identity.sha256:
                raise InvariantRefusalError("runner executable identity changed")
            self._home = self._make_private_dir("home")
            self._docker_config = self._make_private_dir("docker-config")
            self._tmpdir = self._make_private_dir("tmp")
        except BaseException as error:
            try:
                shutil.rmtree(created_root)
            except BaseException as cleanup_error:
                if hasattr(error, "add_note"):
                    error.add_note(
                        "runner private directory cleanup failed: "
                        + type(cleanup_error).__name__
                    )
            raise

    @property
    def executable_identity(self) -> ExecutableIdentity:
        return self._identity

    @property
    def private_environment(self) -> Tuple[Tuple[str, str], ...]:
        """Expose only the deterministic environment for audit/tests."""

        return tuple(sorted(self._environment().items()))

    def _make_private_dir(self, name: str) -> str:
        path = os.path.join(self._private_root, name)
        os.mkdir(path, 0o700)
        os.chmod(path, 0o700)
        return path

    def _environment(self) -> dict:
        # Do not start from os.environ.  In particular, credential helpers,
        # auth variables, SSH agents, Git configuration, and proxy settings do
        # not cross this boundary.
        return {
            "DOCKER_CONFIG": self._docker_config,
            "HOME": self._home,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "TMPDIR": self._tmpdir,
        }

    def cancel(self) -> None:
        self._cancelled.set()

    def close(self) -> None:
        with self._run_lock:
            if self._closed:
                return
            try:
                shutil.rmtree(self._private_root)
            except FileNotFoundError:
                # A prior interrupted close can finish removal immediately
                # before control returns to this object.
                if os.path.lexists(self._private_root):
                    raise
            self._closed = True

    def __enter__(self) -> "SubprocessRunner":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _check_source_identity(self) -> None:
        current = _capture_identity(self._identity.canonical_path)
        if current != self._identity:
            raise InvariantRefusalError("runner executable identity changed")

    def _check_identity(self) -> None:
        self._check_source_identity()
        if _capture_identity(self._bound_executable) != self._bound_identity:
            raise InvariantRefusalError("runner executable identity changed")

    @staticmethod
    def _waitid_observer_available() -> bool:
        return all(
            getattr(os, name, None) is not None
            for name in ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT")
        )

    @staticmethod
    def _kqueue_observer_available() -> bool:
        return all(
            getattr(select, name, None) is not None
            for name in (
                "kqueue",
                "kevent",
                "KQ_FILTER_PROC",
                "KQ_NOTE_EXIT",
                "KQ_EV_ADD",
                "KQ_EV_ENABLE",
            )
        )

    @classmethod
    def _observer_available(cls) -> bool:
        return cls._waitid_observer_available() or cls._kqueue_observer_available()

    @staticmethod
    def _signal_group(
        process: subprocess.Popen,
        process_group: int,
        cleanup: _CleanupState,
    ) -> None:
        """Make the sole process-group signal attempt, without reaping."""

        if cleanup.group_signal_attempted:
            return
        cleanup.group_signal_attempted = True
        if cleanup.identity_lost or process.returncode is not None:
            cleanup.identity_lost = True
            cleanup.cleanup_uncertain = True
            return
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            # The unreaped leader still reserves its PID.  ESRCH therefore
            # means the private group has no signalable members.
            return
        except PermissionError:
            if cleanup.leader_exit_observed:
                # Darwin reports EPERM for a process group whose only member
                # is the non-reaping-observed zombie leader.  Any live
                # descendant created by this non-setuid executable retains
                # our uid and would instead make the group signal succeed.
                return
            cleanup.cleanup_uncertain = True
            try:
                os.kill(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except BaseException:
                cleanup.cleanup_uncertain = True
        except BaseException:
            cleanup.cleanup_uncertain = True
            # A group-wide failure must not prevent bounded leader cleanup.
            # Use os.kill directly: Popen.kill()/send_signal() may poll and
            # reap before sending on POSIX.
            try:
                os.kill(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except BaseException:
                cleanup.cleanup_uncertain = True

    @classmethod
    def _terminate_group(
        cls,
        process: subprocess.Popen,
        process_group: int,
        cleanup: _CleanupState,
    ) -> Optional[int]:
        """Signal once, then make one bounded reap attempt."""

        cls._signal_group(process, process_group, cleanup)
        if cleanup.identity_lost:
            return process.returncode
        try:
            return process.wait(timeout=_REAP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            cleanup.cleanup_uncertain = True
        except BaseException:
            cleanup.cleanup_uncertain = True
        return process.returncode

    @staticmethod
    def _close_pipes(process: subprocess.Popen) -> bool:
        closed = True
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except BaseException:
                closed = False
        return closed

    def run(self, argv: Tuple[str, ...], *, timeout: float) -> CommandResult:
        command = _validate_argv(argv, self._identity.canonical_path)
        if type(timeout) not in (int, float):
            raise UsageStateError("invalid runner timeout")
        try:
            timeout_seconds = float(timeout)
        except OverflowError as error:
            raise UsageStateError("invalid runner timeout") from error
        if (
            not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > _MAX_TIMEOUT_SECONDS
        ):
            raise UsageStateError("invalid runner timeout")
        with self._run_lock:
            if self._closed:
                raise UsageStateError("runner is closed")
            if self._cancelled.is_set():
                raise RunnerFailureError("runner cancelled")
            self._check_identity()
            return self._run_locked(command, timeout_seconds)

    def _run_locked(self, argv: Tuple[str, ...], timeout: float) -> CommandResult:
        if not self._observer_available():
            # Refuse before starting a child.  Pipe EOF is not a process-exit
            # observation and racing it against SIGKILL corrupts successful
            # return codes on older Darwin Python builds.
            raise RunnerFailureError(_OBSERVER_UNAVAILABLE)

        # Initialize every finalizer input before spawning.  Once Popen stores
        # a process, no later bytecode-visible fault can bypass owned cleanup.
        process: Optional[subprocess.Popen] = None
        cleanup = _CleanupState()
        stdout = bytearray()
        stderr = bytearray()
        observer = None
        streams: Optional[selectors.BaseSelector] = None
        failure: Optional[str] = None
        body_error: Optional[BaseException] = None
        cleanup_error_cause: Optional[BaseException] = None
        returncode: Optional[int] = None
        deadline = time.monotonic() + timeout

        try:
            try:
                process = subprocess.Popen(
                    argv,
                    # Execute the private identity-bound copy while preserving
                    # caller-visible argv[0]. This closes the pathname
                    # replacement window between identity verification/exec.
                    executable=self._bound_executable,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    env=self._environment(),
                    cwd=self._tmpdir,
                    close_fds=True,
                    start_new_session=True,
                    text=False,
                )
            except OSError as error:
                raise RunnerFailureError("runner failed to start") from error

            # Register process-exit observation before selector construction,
            # pipe mutation, or any other post-Popen fault point.
            observation = _EXIT_OBSERVER_UNAVAILABLE
            if self._waitid_observer_available():
                observer = _WaitidExitObserver(process)
                observation = observer.register()
                if observation == _EXIT_OBSERVER_UNAVAILABLE:
                    observer.close()
                    observer = None
            if observer is None:
                if not self._kqueue_observer_available():
                    raise RunnerFailureError(_OBSERVER_UNAVAILABLE)
                try:
                    observer = _KqueueExitObserver(process)
                except (AttributeError, NotImplementedError, OSError) as error:
                    raise RunnerFailureError(_OBSERVER_UNAVAILABLE) from error
                # Assignment above gives the outer finalizer ownership before
                # registration, including when register itself is interrupted.
                try:
                    observation = observer.register()
                except (AttributeError, NotImplementedError, OSError) as error:
                    raise RunnerFailureError(_OBSERVER_UNAVAILABLE) from error
            if observation == _EXIT_OBSERVED:
                cleanup.leader_exit_observed = True
                self._signal_group(process, process.pid, cleanup)
            elif observation == _EXIT_IDENTITY_LOST:
                cleanup.identity_lost = True
                cleanup.cleanup_uncertain = True
                raise RunnerFailureError(_CLEANUP_UNCERTAIN)

            streams = selectors.DefaultSelector()
            assert process.stdout is not None
            assert process.stderr is not None
            for stream, destination in (
                (process.stdout, stdout),
                (process.stderr, stderr),
            ):
                os.set_blocking(stream.fileno(), False)
                streams.register(stream, selectors.EVENT_READ, destination)

            while True:
                if self._cancelled.is_set():
                    failure = "runner cancelled"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = "runner timed out"
                    break

                if streams.get_map():
                    events = streams.select(min(_POLL_SECONDS, remaining))
                    for key, _mask in events:
                        stream = key.fileobj
                        destination = key.data
                        try:
                            chunk = os.read(stream.fileno(), _READ_CHUNK_BYTES)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            streams.unregister(stream)
                            stream.close()
                            continue
                        remaining_capacity = self._max_output_bytes - len(destination)
                        if len(chunk) > remaining_capacity:
                            destination.extend(chunk[: max(0, remaining_capacity)])
                            failure = "runner output exceeded limit"
                            break
                        destination.extend(chunk)
                    if failure is not None:
                        break
                elif not cleanup.group_signal_attempted:
                    # No selector is available to provide the short bounded
                    # sleep while waiting for the leader's exit observation.
                    time.sleep(min(_POLL_SECONDS, remaining))

                if not cleanup.group_signal_attempted:
                    observation = observer.observe()
                    if observation == _EXIT_OBSERVED:
                        cleanup.leader_exit_observed = True
                        self._signal_group(process, process.pid, cleanup)
                    elif observation == _EXIT_IDENTITY_LOST:
                        cleanup.identity_lost = True
                        cleanup.cleanup_uncertain = True
                        break
                    elif observation == _EXIT_OBSERVER_UNAVAILABLE:
                        raise RunnerFailureError(_OBSERVER_UNAVAILABLE)

                if not streams.get_map() and cleanup.group_signal_attempted:
                    break
        except BaseException as error:
            body_error = error
        finally:
            if process is not None:
                try:
                    process_group = process.pid
                    returncode = self._terminate_group(
                        process, process_group, cleanup
                    )
                except BaseException as error:
                    cleanup.cleanup_uncertain = True
                    cleanup_error_cause = error
                if streams is not None:
                    try:
                        streams.close()
                    except BaseException as error:
                        cleanup.cleanup_uncertain = True
                        if cleanup_error_cause is None:
                            cleanup_error_cause = error
                if observer is not None:
                    try:
                        observer.close()
                    except BaseException as error:
                        cleanup.cleanup_uncertain = True
                        if cleanup_error_cause is None:
                            cleanup_error_cause = error
                if not self._close_pipes(process):
                    cleanup.cleanup_uncertain = True

        if cleanup.cleanup_uncertain:
            cleanup_error = RunnerFailureError(_CLEANUP_UNCERTAIN)
            cause: Optional[BaseException] = body_error
            if cause is None and failure is not None:
                cause = RunnerFailureError(failure)
            if cause is None:
                cause = cleanup_error_cause
            if cause is not None and not isinstance(cause, Exception):
                if hasattr(cause, "add_note"):
                    cause.add_note(_CLEANUP_UNCERTAIN)
                raise cause
            if cause is not None:
                raise cleanup_error from cause
            raise cleanup_error
        if body_error is not None:
            raise body_error
        if failure is not None:
            raise RunnerFailureError(failure)
        if returncode is None:
            raise RunnerFailureError(_CLEANUP_UNCERTAIN)
        if returncode != 0:
            raise RunnerFailureError("runner command failed")

        # Decoding and result construction occur only after owned process
        # resources are already finalized, so their exceptions cannot leak a
        # child, descendant, selector, or pipe.
        return CommandResult(
            argv=argv,
            returncode=returncode,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )


__all__ = [
    "CommandResult",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "ExecutableIdentity",
    "Runner",
    "SubprocessRunner",
]
