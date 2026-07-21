"""Bounded one-shot subprocess execution for plain and single-JSON CLIs."""

from __future__ import annotations

import os
import selectors
import signal
import stat
import subprocess
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

from ..errors import (
    ConfigurationError,
    LimitExceeded,
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
    validated_workspace,
    validate_positive_timeout,
)


@dataclass(frozen=True)
class FixedProcessResult:
    """Bounded, secret-redacted result from one fixed argv execution."""

    returncode: int
    stdout: str
    stderr: str


@contextmanager
def _managed_environment(
    environment: IsolatedEnvironment,
) -> Iterator[IsolatedEnvironment]:
    """Own partial entry and retry cleanup before a stack-local owner is lost."""

    primary_failure = None
    entered = False
    try:
        entered_environment = environment.__enter__()
        entered = True
        yield entered_environment
    except BaseException as caught:
        primary_failure = caught
        raise
    finally:
        cleanup_failure = None
        # __enter__ can fail after acquiring only part of HOME/TMPDIR.  Since
        # fixed/interactive APIs cannot return that owner, perform bounded
        # retries here even when context entry itself failed.
        for _ in range(4):
            if not environment.has_resources:
                break
            try:
                environment._cleanup()
            except BaseException as caught:
                if cleanup_failure is None:
                    cleanup_failure = caught
        if environment.has_resources:
            failure = TransportError(
                "isolated provider environment cleanup is incomplete"
            )
            failure.__cause__ = cleanup_failure
            if primary_failure is not None:
                raise failure from primary_failure
            raise failure
        if entered and primary_failure is None and cleanup_failure is not None:
            raise cleanup_failure


def _argv(value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ConfigurationError("subprocess argv must be a nonempty string sequence")
    result = []
    try:
        for index, item in enumerate(value):
            if index >= 1024:
                raise ConfigurationError("subprocess argv exceeds 1024 items")
            if type(item) is not str or not item or "\x00" in item:
                raise ConfigurationError("subprocess argv contains an invalid item")
            result.append(item)
    except ConfigurationError:
        raise
    except Exception:
        raise ConfigurationError("subprocess argv is malformed") from None
    if not result:
        raise ConfigurationError("subprocess argv must be nonempty")
    return tuple(result)


def _require_nonreaping_process_observation() -> None:
    """Require the POSIX primitive used to retain a child's PID identity.

    ``Popen.poll()`` and ``Popen.wait()`` reap an exited child.  Reaping before
    the dedicated process group has received its final signal permits the
    numeric PID/PGID to be reused.  Linux and macOS both expose ``waitid`` with
    ``WNOWAIT``; fail closed instead of falling back to a reaping observation.
    """

    required = (
        "waitid",
        "P_PID",
        "WEXITED",
        "WNOHANG",
        "WNOWAIT",
        "CLD_EXITED",
        "CLD_KILLED",
        "CLD_DUMPED",
    )
    if os.name != "posix" or any(getattr(os, name, None) is None for name in required):
        raise UnsupportedPlatformError(
            "subprocess transports require POSIX non-reaping child observation"
        )


def _observe_process_returncode_nonreaping(
    process: subprocess.Popen,
) -> Optional[int]:
    """Return an exited child's status without consuming its wait status."""

    if process.returncode is not None:
        # Another final-reap path already completed.  Callers must not signal a
        # process group after this point.
        return process.returncode
    _require_nonreaping_process_observation()
    options = os.WEXITED | os.WNOHANG | os.WNOWAIT
    while True:
        try:
            result = os.waitid(os.P_PID, process.pid, options)
            break
        except InterruptedError:
            continue
        except (ChildProcessError, OSError) as caught:
            if process.returncode is not None:
                return process.returncode
            raise TransportError(
                "provider subprocess could not be observed without reaping"
            ) from caught
    if result is None or getattr(result, "si_pid", 0) == 0:
        return None
    if result.si_pid != process.pid:
        raise TransportError(
            "provider subprocess returned an unexpected non-reaping wait result"
        )
    if result.si_code == os.CLD_EXITED:
        return int(result.si_status)
    if result.si_code in (os.CLD_KILLED, os.CLD_DUMPED):
        return -int(result.si_status)
    raise TransportError(
        "provider subprocess returned an unsupported non-reaping wait result"
    )


def _wait_for_process_exit_nonreaping(
    process: subprocess.Popen, timeout: float
) -> Optional[int]:
    """Boundedly observe exit while leaving the leader waitable and unreaped."""

    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        returncode = _observe_process_returncode_nonreaping(process)
        if returncode is not None:
            return returncode
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(0.01, remaining))


def _signal_owned_process_tree(process: subprocess.Popen, sig: signal.Signals) -> None:
    """Signal a dedicated provider process group with a leader fallback."""

    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, sig)
        return
    except (OSError, ProcessLookupError, PermissionError):
        pass
    # A group lookup can race with exec/exit or fail on a constrained POSIX
    # host.  Never strand the leader merely because killpg failed.
    if process.returncode is not None:
        return
    try:
        os.kill(process.pid, sig)
    except (OSError, ProcessLookupError):
        pass


def _wait_bounded(process: subprocess.Popen, timeout: float) -> Optional[int]:
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def _terminate_owned_process_tree(process: subprocess.Popen) -> int:
    """Bound TERM/KILL escalation and return only after the leader is reaped."""

    if process.returncode is not None:
        return process.returncode
    _signal_owned_process_tree(process, signal.SIGTERM)
    observation_failed = False
    try:
        observed_returncode = _wait_for_process_exit_nonreaping(process, 0.2)
    except BaseException:
        # Safe observation was preflighted before spawn, but fault injection or
        # a platform defect must not prevent the final KILL and reap sequence.
        observation_failed = True
        observed_returncode = None
    # Signal KILL even after observing leader exit: the unreaped leader retains
    # its numeric identity while ordinary descendants may still own inherited
    # descriptors in the dedicated group.
    _signal_owned_process_tree(process, signal.SIGKILL)
    if observed_returncode is None and not observation_failed:
        try:
            _wait_for_process_exit_nonreaping(process, 0.8)
        except BaseException:
            pass
    # Every signal has now been delivered.  This is the sole reap point.
    returncode = _wait_bounded(process, 0.2)
    if returncode is None:
        raise TransportError("provider subprocess could not be reaped after termination")
    return returncode


def _interactive_fd(stream: object, label: str) -> tuple[int, tuple[int, int]]:
    try:
        descriptor = stream.fileno()  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        raise ConfigurationError("interactive auth {} is unavailable".format(label)) from None
    try:
        status = os.fstat(descriptor)
    except (OSError, TypeError, ValueError):
        raise ConfigurationError("interactive auth {} is unavailable".format(label)) from None
    if (
        type(descriptor) is not int
        or descriptor < 0
        or not stat.S_ISCHR(status.st_mode)
        or not os.isatty(descriptor)
    ):
        raise ConfigurationError("interactive auth requires TTY stdin/stdout/stderr")
    return descriptor, (status.st_dev, status.st_rdev)


def _cleanup_spawned_process(
    process: subprocess.Popen,
    *,
    selector: Optional[selectors.BaseSelector] = None,
    executable_identity: Optional[ExecutableIdentity] = None,
    original_error: Optional[BaseException] = None,
) -> int:
    """Always reap a spawned child and release runtime-owned resources.

    An already-active exception remains authoritative unless bounded cleanup
    cannot confirm that the provider leader was reaped.  Close/identity
    failures are still surfaced on the successful path.
    """

    reap_error = None
    cleanup_error = None
    returncode = None
    try:
        returncode = _terminate_owned_process_tree(process)
    except TransportError as caught:
        reap_error = caught
    except BaseException as caught:
        reap_error = TransportError(
            "provider subprocess could not be reaped after termination"
        )
        reap_error.__cause__ = caught
    if selector is not None:
        try:
            selector.close()
        except BaseException as caught:
            cleanup_error = caught
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except BaseException as caught:
                if cleanup_error is None:
                    cleanup_error = caught
    if executable_identity is not None:
        try:
            executable_identity.verify_metadata()
        except BaseException as caught:
            if cleanup_error is None:
                cleanup_error = caught
    if reap_error is not None:
        if original_error is not None:
            raise reap_error from original_error
        raise reap_error
    if original_error is None and cleanup_error is not None:
        raise cleanup_error
    assert returncode is not None
    return returncode


def _run_interactive_process(
    argv: Sequence[str],
    *,
    timeout: float,
    cwd: str,
    provider_env: Optional[Mapping[str, str]],
    allowed_provider_env: Sequence[str],
    persistent_home: str,
    cancellation: Optional[CancellationToken],
    executable_identity: ExecutableIdentity,
    stdin: object,
    stdout: object,
    stderr: object,
) -> int:
    """Run one runtime-owned fixed auth command on caller-provided TTYs."""

    clean_argv = _argv(argv)
    if os.name != "posix":
        raise UnsupportedPlatformError(
            "interactive auth requires POSIX process-group cleanup"
        )
    _require_nonreaping_process_observation()
    deadline_seconds = validate_positive_timeout(timeout)
    clean_cwd = validated_workspace(cwd)
    if type(executable_identity) is not ExecutableIdentity:
        raise ConfigurationError("executable_identity must be ExecutableIdentity")
    _require_executable_identity_argv(clean_argv[0], executable_identity)
    token = cancellation if cancellation is not None else CancellationToken()
    if type(token) is not CancellationToken:
        raise ConfigurationError("cancellation must be CancellationToken")
    stdin_fd, stdin_tty = _interactive_fd(stdin, "stdin")
    stdout_fd, stdout_tty = _interactive_fd(stdout, "stdout")
    stderr_fd, stderr_tty = _interactive_fd(stderr, "stderr")
    if len({stdin_tty, stdout_tty, stderr_tty}) != 1:
        raise ConfigurationError(
            "interactive auth stdin/stdout/stderr must use the same TTY"
        )
    token.raise_if_cancelled()
    with DirectoryPin(clean_cwd) as cwd_pin:
        environment = IsolatedEnvironment(
            provider_env,
            allowed_provider_keys=allowed_provider_env,
            persistent_home=persistent_home,
        )
        with _managed_environment(environment):
            token.raise_if_cancelled()
            executable_identity.verify()
            cwd_pin.verify()
            environment.verify_for_spawn()
            token.raise_if_cancelled()
            try:
                process = subprocess.Popen(
                    list(clean_argv),
                    stdin=stdin_fd,
                    stdout=stdout_fd,
                    stderr=stderr_fd,
                    cwd=cwd_pin,
                    env=environment.env,
                    shell=False,
                    start_new_session=True,
                    close_fds=True,
                )
            except (OSError, UnicodeError):
                raise TransportError("failed to start interactive auth subprocess") from None
            try:
                cwd_pin.verify()
                environment.verify_after_spawn()
                executable_identity.verify_metadata()
                deadline = time.monotonic() + deadline_seconds
                failure = None
                while _observe_process_returncode_nonreaping(process) is None:
                    if token.cancelled:
                        failure = TransportCancelled("extension operation cancelled")
                        break
                    if time.monotonic() >= deadline:
                        failure = TransportTimeout("interactive auth subprocess timed out")
                        break
                    time.sleep(0.01)
                if failure is not None:
                    raise failure
            except BaseException as caught:
                _cleanup_spawned_process(
                    process,
                    executable_identity=executable_identity,
                    original_error=caught,
                )
                raise
            return _cleanup_spawned_process(
                process,
                executable_identity=executable_identity,
            )


def run_fixed_process(
    argv: Sequence[str],
    *,
    executable_identity: ExecutableIdentity,
    stdin_text: Optional[str] = None,
    timeout: float = 30.0,
    cwd: Optional[str] = None,
    provider_env: Optional[Mapping[str, str]] = None,
    allowed_provider_env: Sequence[str] = (),
    persistent_home: Optional[str] = None,
    limits: TransportLimits = TransportLimits(),
    cancellation: Optional[CancellationToken] = None,
) -> FixedProcessResult:
    """Execute exactly one argv with ``shell=False`` and bounded I/O.

    This is intentionally not a general command or browser-launch API.  The
    adapter contract supplies the fixed argv and the caller may supply only a
    validated prompt on stdin.
    """

    clean_argv = _argv(argv)
    if os.name != "posix":
        raise UnsupportedPlatformError(
            "subprocess transports require POSIX process-group cleanup"
        )
    _require_nonreaping_process_observation()
    deadline_seconds = validate_positive_timeout(timeout)
    if type(limits) is not TransportLimits:
        raise ConfigurationError("limits must be TransportLimits")
    token = cancellation if cancellation is not None else CancellationToken()
    if type(token) is not CancellationToken:
        raise ConfigurationError("cancellation must be CancellationToken")
    if type(executable_identity) is not ExecutableIdentity:
        raise ConfigurationError("executable_identity must be ExecutableIdentity")
    _require_executable_identity_argv(clean_argv[0], executable_identity)
    if cwd is None:
        raise ConfigurationError("subprocess cwd must be an explicit provider workspace")
    clean_cwd = validated_workspace(cwd)
    if stdin_text is not None:
        if type(stdin_text) is not str or "\x00" in stdin_text:
            raise ConfigurationError("subprocess stdin text is invalid")
        try:
            stdin_bytes = stdin_text.encode("utf-8", "strict")
        except UnicodeError:
            raise ConfigurationError("subprocess stdin text is invalid") from None
        if len(stdin_bytes) > limits.max_output_bytes:
            raise LimitExceeded("subprocess stdin exceeds configured limit")
    else:
        stdin_bytes = None

    token.raise_if_cancelled()
    environment = IsolatedEnvironment(
        provider_env,
        allowed_provider_keys=allowed_provider_env,
        persistent_home=persistent_home,
    )
    identity_before = executable_identity
    with DirectoryPin(clean_cwd) as cwd_pin:
        with _managed_environment(environment):
            token.raise_if_cancelled()
            identity_before.verify()
            cwd_pin.verify()
            environment.verify_for_spawn()
            token.raise_if_cancelled()
            stdout = bytearray()
            stderr = bytearray()
            failure = None
            selector = None
            try:
                process = subprocess.Popen(
                    list(clean_argv),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd_pin,
                    env=environment.env,
                    shell=False,
                    start_new_session=True,
                    close_fds=True,
                )
            except (OSError, UnicodeError):
                raise TransportError("failed to start extension subprocess") from None
            try:
                cwd_pin.verify()
                environment.verify_after_spawn()
                identity_before.verify_metadata()
                try:
                    assert process.stdin is not None
                    assert process.stdout is not None and process.stderr is not None
                    selector = selectors.DefaultSelector()
                    read_streams = {
                        process.stdout.fileno(): (
                            process.stdout,
                            stdout,
                            limits.max_output_bytes,
                            "stdout",
                        ),
                        process.stderr.fileno(): (
                            process.stderr,
                            stderr,
                            limits.max_stderr_bytes,
                            "stderr",
                        ),
                    }
                    for descriptor, values in read_streams.items():
                        os.set_blocking(descriptor, False)
                        selector.register(descriptor, selectors.EVENT_READ, values)
                    stdin_descriptor = process.stdin.fileno()
                    stdin_offset = 0
                    if stdin_bytes:
                        os.set_blocking(stdin_descriptor, False)
                        selector.register(
                            stdin_descriptor,
                            selectors.EVENT_WRITE,
                            (process.stdin, None, None, "stdin"),
                        )
                    else:
                        process.stdin.close()
                except Exception:
                    raise TransportError(
                        "failed to initialize extension subprocess pipes"
                    ) from None

                assert selector is not None
                deadline = time.monotonic() + deadline_seconds
                leader_exited_at = None
                while True:
                    now = time.monotonic()
                    if token.cancelled:
                        failure = TransportCancelled("extension operation cancelled")
                        break
                    if now >= deadline:
                        failure = TransportTimeout("extension subprocess timed out")
                        break
                    leader_exited = (
                        _observe_process_returncode_nonreaping(process) is not None
                    )
                    if leader_exited and leader_exited_at is None:
                        leader_exited_at = now
                    registered_reads = any(
                        key.events & selectors.EVENT_READ
                        for key in selector.get_map().values()
                    )
                    if leader_exited and not registered_reads:
                        break
                    # A detached descendant may retain inherited pipe descriptors
                    # outside the owned group. Never wait indefinitely for its EOF.
                    if leader_exited_at is not None and now - leader_exited_at >= 0.2:
                        break
                    events = selector.select(
                        timeout=min(0.02, max(0.001, deadline - now))
                    )
                    for key, mask in events:
                        descriptor = key.fd
                        stream, target, maximum, label = key.data
                        if mask & selectors.EVENT_WRITE:
                            assert stdin_bytes is not None
                            try:
                                written = os.write(
                                    descriptor,
                                    stdin_bytes[stdin_offset : stdin_offset + 65536],
                                )
                            except BlockingIOError:
                                continue
                            except (BrokenPipeError, OSError):
                                selector.unregister(descriptor)
                                stream.close()
                                continue
                            stdin_offset += written
                            if stdin_offset >= len(stdin_bytes):
                                selector.unregister(descriptor)
                                stream.close()
                            continue
                        try:
                            chunk = os.read(descriptor, 65536)
                        except BlockingIOError:
                            continue
                        except OSError:
                            failure = TransportError(
                                "failed to read extension subprocess output"
                            )
                            break
                        if not chunk:
                            selector.unregister(descriptor)
                            continue
                        remaining = maximum - len(target)
                        if remaining > 0:
                            target.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            failure = LimitExceeded(
                                "subprocess {} exceeds configured limit".format(label)
                            )
                            break
                    if failure is not None:
                        break
                if failure is not None:
                    raise failure
            except BaseException as caught:
                _cleanup_spawned_process(
                    process,
                    selector=selector,
                    executable_identity=identity_before,
                    original_error=caught,
                )
                raise
            returncode = _cleanup_spawned_process(
                process,
                selector=selector,
                executable_identity=identity_before,
            )

    try:
        stdout_text = bytes(stdout).decode("utf-8", "strict")
    except UnicodeError:
        raise ProtocolError("extension subprocess stdout is not valid UTF-8") from None
    stderr_text = redact_diagnostics(
        bytes(stderr).decode("utf-8", "replace"),
        environment.secret_values,
    )
    return FixedProcessResult(returncode, stdout_text, stderr_text)


__all__ = ["FixedProcessResult", "run_fixed_process"]
