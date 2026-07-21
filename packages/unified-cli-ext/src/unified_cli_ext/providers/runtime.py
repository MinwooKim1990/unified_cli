"""Explicit runtime operations for the provider-adapter ABI.

Nothing in this module runs at import time.  Callers must name a binary path
and explicitly request each probe.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import re
import sys
import threading
import unicodedata
from dataclasses import dataclass, fields, is_dataclass
from types import MappingProxyType
from typing import Any, FrozenSet, Mapping, Optional, Tuple

from ..errors import ConfigurationError, ProcessFailed, ProtocolError, TransportError
from ..normalization import SessionRef, freeze_json
from ..transports import (
    CancellationToken,
    ExecutableIdentity,
    FixedProcessResult,
    JsonRpcProcessClient,
    JsonlProcess,
    private_persistent_home,
    run_fixed_process,
    validated_workspace,
)
from ..transports.process import _run_interactive_process
from ..transports.security import (
    _MAX_INTERPRETER_DEPTH,
    _OwnedTemporaryDirectory,
    strict_json_loads,
)
from .contract import (
    AdapterDescriptorV1,
    AdapterStatus,
    BuiltPromptInvocation,
    DeclarativeProbeSpec,
    ExitStatusProbeSpec,
    FixedCommandSpec,
    JsonProbeSpec,
    OperationLimits,
    PlainTextFieldSpec,
    PlainTextProbeSpec,
    ProbeFormat,
    ProviderAdapterSpecV1,
    TransportKind,
    describe_adapter,
)


_VERSION_RE = re.compile(
    r"^(?P<release>(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){0,3})"
    r"(?P<prerelease>-(?:[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?:[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_FEATURE_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_MAX_MODEL_ID_CHARS = 512


def _cleanup_temporary(temporary: Any) -> None:
    if type(temporary) is not _OwnedTemporaryDirectory:
        raise ConfigurationError("runtime temporary owner is invalid")
    temporary.cleanup()


def _cleanup_complete(owner: Any) -> bool:
    if type(owner) is _OwnedTemporaryDirectory:
        return owner.cleaned
    if type(owner) is JsonlProcess:
        return owner._resources_complete()
    if type(owner) is JsonRpcProcessClient:
        return owner._transport._resources_complete()
    return False


def _cleanup_once(owner: Any) -> None:
    if type(owner) is _OwnedTemporaryDirectory:
        owner.cleanup()
    elif type(owner) in (JsonlProcess, JsonRpcProcessClient):
        owner.close()
    else:
        raise ConfigurationError("pending cleanup owner is invalid")


class _PendingCleanupBundle:
    """Ordered ownership retained when a factory cannot return its resources."""

    __slots__ = ("_lock", "_owners")

    def __init__(self, owners: Tuple[Any, ...]) -> None:
        self._owners = list(owners)
        self._lock = threading.RLock()

    @property
    def complete(self) -> bool:
        with self._lock:
            return not self._owners

    def close(self) -> None:
        with self._lock:
            while self._owners:
                owner = self._owners[0]
                try:
                    _cleanup_once(owner)
                except BaseException:
                    if not _cleanup_complete(owner):
                        raise
                if not _cleanup_complete(owner):
                    raise TransportError("runtime cleanup owner remains active")
                self._owners.pop(0)


_PENDING_CLEANUP_LIMIT = 32
_PENDING_CLEANUP_LOCK = threading.RLock()
_PENDING_CLEANUPS = []
_PENDING_CLEANUP_RESERVATIONS = 0
_PENDING_ATEXIT_REGISTERED = False


def _drain_pending_at_exit() -> None:
    try:
        drain_pending_cleanups(max_passes=4)
    except BaseException:
        pass


def _retain_pending_cleanup(
    owner: _PendingCleanupBundle, *, reserved: bool = False
) -> None:
    global _PENDING_ATEXIT_REGISTERED, _PENDING_CLEANUP_RESERVATIONS
    if owner.complete:
        return
    with _PENDING_CLEANUP_LOCK:
        if reserved:
            if _PENDING_CLEANUP_RESERVATIONS <= 0:
                raise TransportError("runtime cleanup reservation is invalid")
            _PENDING_CLEANUP_RESERVATIONS -= 1
        elif len(_PENDING_CLEANUPS) + _PENDING_CLEANUP_RESERVATIONS >= _PENDING_CLEANUP_LIMIT:
            raise TransportError("runtime pending cleanup registry is full")
        _PENDING_CLEANUPS.append(owner)
        if not _PENDING_ATEXIT_REGISTERED:
            atexit.register(_drain_pending_at_exit)
            _PENDING_ATEXIT_REGISTERED = True


def drain_pending_cleanups(*, max_passes: int = 4) -> int:
    """Synchronously retry retained factory cleanup and return owner count."""

    if type(max_passes) is not int or not 1 <= max_passes <= 16:
        raise ConfigurationError("cleanup drain passes must be between one and sixteen")
    for _ in range(max_passes):
        with _PENDING_CLEANUP_LOCK:
            snapshot = tuple(_PENDING_CLEANUPS)
        if not snapshot:
            return 0
        progress = False
        for owner in snapshot:
            try:
                owner.close()
            except BaseException:
                pass
            if owner.complete:
                with _PENDING_CLEANUP_LOCK:
                    try:
                        _PENDING_CLEANUPS.remove(owner)
                    except ValueError:
                        pass
                    else:
                        progress = True
        if not progress:
            break
    with _PENDING_CLEANUP_LOCK:
        return len(_PENDING_CLEANUPS)


def _reserve_pending_cleanup_slot() -> None:
    global _PENDING_CLEANUP_RESERVATIONS
    drain_pending_cleanups(max_passes=1)
    with _PENDING_CLEANUP_LOCK:
        if len(_PENDING_CLEANUPS) + _PENDING_CLEANUP_RESERVATIONS >= _PENDING_CLEANUP_LIMIT:
            raise TransportError("runtime pending cleanup registry is full")
        _PENDING_CLEANUP_RESERVATIONS += 1


def _release_pending_cleanup_slot() -> None:
    global _PENDING_CLEANUP_RESERVATIONS
    with _PENDING_CLEANUP_LOCK:
        if _PENDING_CLEANUP_RESERVATIONS <= 0:
            raise TransportError("runtime cleanup reservation is invalid")
        _PENDING_CLEANUP_RESERVATIONS -= 1


def _cleanup_or_retain(
    owners: Tuple[Any, ...],
    primary_failure: Optional[BaseException] = None,
    *,
    reserved: bool = False,
) -> None:
    bundle = _PendingCleanupBundle(owners)
    first_failure = None
    for _ in range(4):
        if bundle.complete:
            break
        try:
            bundle.close()
        except BaseException as caught:
            if first_failure is None:
                first_failure = caught
    if not bundle.complete:
        _retain_pending_cleanup(bundle, reserved=reserved)
        if primary_failure is None:
            failure = TransportError("runtime cleanup is retained for bounded retry")
            failure.__cause__ = first_failure
            raise failure
    else:
        if reserved:
            _release_pending_cleanup_slot()
        if primary_failure is None and first_failure is not None:
            raise first_failure


def _regular_binary(path: str, executable_name: str) -> "BinaryProvenance":
    if type(path) is not str or not path or "\x00" in path or len(path) > 16 * 1024:
        raise ConfigurationError("provider binary path is invalid")
    if not os.path.isabs(path):
        raise ConfigurationError("provider binary path must be absolute")
    invoked = os.path.normpath(path)
    if os.path.basename(invoked) != executable_name:
        raise ConfigurationError("provider binary basename does not match adapter metadata")
    real_path = os.path.realpath(invoked)
    if real_path != invoked:
        raise ConfigurationError("provider binary path must be canonical and non-symlinked")
    identity = ExecutableIdentity.capture(real_path)
    return BinaryProvenance(
        invoked_path=invoked,
        real_path=real_path,
        sha256=identity.sha256,
        device=identity.device,
        inode=identity.inode,
        size=identity.size,
        mtime_ns=identity.mtime_ns,
        mode=identity.mode,
        owner=identity.owner,
        parent_chain=identity.parent_chain,
        interpreter=(
            None
            if identity.interpreter is None
            else _identity_from_record(_identity_record(identity.interpreter))
        ),
        ctime_ns=identity.ctime_ns,
    )


@dataclass(frozen=True)
class BinaryProvenance:
    """Legacy API name for a local executable identity, not official provenance.

    SHA-256 detects local replacement only.  Official package provenance and
    acquisition receipts remain requirements of each real adapter layer.  A
    same-UID process able to rename within the executable's directory retains
    a narrow pathname race between final verification and portable ``execve``.
    """

    invoked_path: str
    real_path: str
    sha256: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    mode: int
    owner: int = -1
    parent_chain: Tuple[Tuple[str, int, int, int, int], ...] = ()
    interpreter: Optional[ExecutableIdentity] = None
    ctime_ns: int = -1

    def verify(self, executable_name: str) -> None:
        if (
            type(executable_name) is not str
            or self.invoked_path != self.real_path
            or not os.path.isabs(self.real_path)
            or os.path.normpath(self.real_path) != self.real_path
            or os.path.realpath(self.real_path) != self.real_path
            or os.path.basename(self.real_path) != executable_name
        ):
            raise ConfigurationError("provider binary provenance changed")
        try:
            self.executable_identity().verify_metadata()
        except ConfigurationError:
            raise ConfigurationError("provider binary provenance changed") from None

    def executable_identity(self) -> ExecutableIdentity:
        return ExecutableIdentity(
            path=self.real_path,
            sha256=self.sha256,
            device=self.device,
            inode=self.inode,
            size=self.size,
            mtime_ns=self.mtime_ns,
            mode=self.mode,
            owner=self.owner,
            parent_chain=self.parent_chain,
            interpreter=(
                None
                if self.interpreter is None
                else _identity_from_record(_identity_record(self.interpreter))
            ),
            ctime_ns=self.ctime_ns,
        )


@dataclass(frozen=True)
class AdapterInspectionV1:
    id: str
    version: str
    features: FrozenSet[str]
    binary: BinaryProvenance
    abi_version: int = 1


def _invocation_record(
    invocation: BuiltPromptInvocation,
) -> Tuple[Tuple[str, ...], Optional[str], Optional[str]]:
    if type(invocation) is not BuiltPromptInvocation:
        raise ConfigurationError("provider invocation state is invalid")
    return (tuple(invocation.argv), invocation.stdin_text, invocation.protocol_text)


def _invocation_from_record(
    record: Tuple[Tuple[str, ...], Optional[str], Optional[str]]
) -> BuiltPromptInvocation:
    return BuiltPromptInvocation(tuple(record[0]), record[1], record[2])


def _identity_record(identity: ExecutableIdentity, depth: int = 0) -> Tuple[Any, ...]:
    if type(identity) is not ExecutableIdentity:
        raise ConfigurationError("provider executable identity state is invalid")
    if depth > _MAX_INTERPRETER_DEPTH:
        raise ConfigurationError("executable interpreter chain exceeds maximum depth")
    return (
        identity.path,
        identity.sha256,
        identity.device,
        identity.inode,
        identity.size,
        identity.mtime_ns,
        identity.mode,
        identity.owner,
        identity.parent_chain,
        (
            None
            if identity.interpreter is None
            else _identity_record(identity.interpreter, depth + 1)
        ),
        identity.ctime_ns,
    )


def _identity_from_record(record: Tuple[Any, ...], depth: int = 0) -> ExecutableIdentity:
    if depth > _MAX_INTERPRETER_DEPTH:
        raise ConfigurationError("executable interpreter chain exceeds maximum depth")
    return ExecutableIdentity(
        path=record[0],
        sha256=record[1],
        device=record[2],
        inode=record[3],
        size=record[4],
        mtime_ns=record[5],
        mode=record[6],
        owner=record[7],
        parent_chain=record[8],
        interpreter=(
            None
            if len(record) < 10 or record[9] is None
            else _identity_from_record(record[9], depth + 1)
        ),
        ctime_ns=-1 if len(record) < 11 else record[10],
    )


def _limits_record(limits: OperationLimits) -> Tuple[float, int, int, int]:
    if type(limits) is not OperationLimits:
        raise ConfigurationError("provider operation limits state is invalid")
    return (
        limits.timeout_seconds,
        limits.max_stdout_bytes,
        limits.max_stderr_bytes,
        limits.max_events,
    )


def _limits_from_record(record: Tuple[float, int, int, int]) -> OperationLimits:
    return OperationLimits(
        timeout_seconds=record[0],
        max_stdout_bytes=record[1],
        max_stderr_bytes=record[2],
        max_events=record[3],
    )


class OpenedProcessTransportV1:
    """One-shot PLAIN/JSON prompt boundary with runtime-owned identity checks."""

    __slots__ = (
        "_kind",
        "_invocation",
        "_binary_identity",
        "_provider_env",
        "_allowed_provider_env",
        "_provider_home",
        "_limits",
        "_cancellation",
        "_cwd",
        "_issued_state",
        "_state_lock",
        "_run_state",
    )

    def __init__(
        self,
        kind: TransportKind,
        invocation: BuiltPromptInvocation,
        binary_identity: ExecutableIdentity,
        provider_env: Mapping[str, str],
        allowed_provider_env: Tuple[str, ...],
        provider_home: Optional[str],
        limits: Any,
        cancellation: Optional[CancellationToken],
        cwd: str,
    ) -> None:
        invocation_copy = _invocation_from_record(_invocation_record(invocation))
        identity_copy = _identity_from_record(_identity_record(binary_identity))
        environment_copy = MappingProxyType(dict(provider_env))
        allowed_copy = tuple(allowed_provider_env)
        limits_copy = _limits_from_record(_limits_record(limits))
        object.__setattr__(self, "_kind", kind)
        object.__setattr__(self, "_invocation", invocation_copy)
        object.__setattr__(self, "_binary_identity", identity_copy)
        object.__setattr__(self, "_provider_env", environment_copy)
        object.__setattr__(self, "_allowed_provider_env", allowed_copy)
        object.__setattr__(self, "_provider_home", provider_home)
        object.__setattr__(self, "_limits", limits_copy)
        object.__setattr__(self, "_cancellation", cancellation)
        object.__setattr__(self, "_cwd", cwd)
        state_lock = threading.RLock()
        object.__setattr__(self, "_state_lock", state_lock)
        object.__setattr__(
            self,
            "_issued_state",
            (
                kind,
                _invocation_record(invocation_copy),
                _identity_record(identity_copy),
                tuple(environment_copy.items()),
                allowed_copy,
                provider_home,
                _limits_record(limits_copy),
                cancellation,
                cwd,
                id(state_lock),
            ),
        )
        object.__setattr__(self, "_run_state", "unused")

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("provider transport handle is read-only")

    @property
    def kind(self) -> TransportKind:
        return self._kind

    @property
    def invocation(self) -> BuiltPromptInvocation:
        return _invocation_from_record(self._issued_state[1])

    def _validate_issued_state(self) -> None:
        try:
            current = (
                self._kind,
                _invocation_record(self._invocation),
                _identity_record(self._binary_identity),
                tuple(self._provider_env.items()),
                tuple(self._allowed_provider_env),
                self._provider_home,
                _limits_record(self._limits),
                self._cancellation,
                self._cwd,
            )
        except BaseException:
            raise ConfigurationError(
                "provider transport handle state changed after issuance"
            ) from None
        if (
            current != self._issued_state[:-1]
            or self._cancellation is not self._issued_state[7]
            or id(self._state_lock) != self._issued_state[9]
        ):
            raise ConfigurationError(
                "provider transport handle state changed after issuance"
            )

    def run(self) -> FixedProcessResult:
        with self._state_lock:
            self._validate_issued_state()
            if self._run_state != "unused":
                raise ConfigurationError("one-shot provider transport is already used")
            object.__setattr__(self, "_run_state", "running")
            invocation = _invocation_from_record(self._issued_state[1])
            identity = _identity_from_record(self._issued_state[2])
            limits = _limits_from_record(self._issued_state[6])
            try:
                return run_fixed_process(
                    invocation.argv,
                    stdin_text=invocation.stdin_text,
                    timeout=limits.timeout_seconds,
                    cwd=self._issued_state[8],
                    provider_env=dict(self._issued_state[3]),
                    allowed_provider_env=self._issued_state[4],
                    persistent_home=self._issued_state[5],
                    limits=limits.transport_limits(),
                    cancellation=self._issued_state[7],
                    executable_identity=identity,
                )
            finally:
                object.__setattr__(self, "_run_state", "used")
                self.close()

    def close(self) -> None:
        return None

    def __enter__(self) -> "OpenedProcessTransportV1":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def __repr__(self) -> str:
        return "<OpenedProcessTransportV1 kind={}>".format(self._kind.value)


class ProtocolLaunchBoundaryV1:
    """Typed boundary that deliberately does not invent a provider protocol."""

    __slots__ = (
        "_kind",
        "_transport",
        "_prompt",
        "_temporary",
        "_binary_identity",
        "_owned_transport",
        "_owned_temporary",
        "_identity_snapshot",
        "_issued_state",
        "_temporary_owner_id",
        "_temporary_cleaned",
        "_close_lock",
        "_closed",
    )

    def __init__(
        self,
        kind: TransportKind,
        transport: Any,
        prompt: str,
        temporary: Any = None,
        binary_identity: Optional[ExecutableIdentity] = None,
    ) -> None:
        if kind is TransportKind.JSONL:
            if type(transport) is not JsonlProcess:
                raise ConfigurationError("JSONL launch boundary transport is invalid")
        elif kind is TransportKind.JSON_RPC:
            if type(transport) is not JsonRpcProcessClient:
                raise ConfigurationError("JSON-RPC launch boundary transport is invalid")
        else:
            raise ConfigurationError("protocol launch boundary kind is invalid")
        if type(prompt) is not str:
            raise ConfigurationError("protocol launch prompt is invalid")
        if temporary is not None and type(temporary) is not _OwnedTemporaryDirectory:
            raise ConfigurationError("protocol launch temporary owner is invalid")
        identity_copy = (
            None
            if binary_identity is None
            else _identity_from_record(_identity_record(binary_identity))
        )
        object.__setattr__(self, "_kind", kind)
        object.__setattr__(self, "_transport", transport)
        object.__setattr__(self, "_prompt", prompt)
        object.__setattr__(self, "_temporary", temporary)
        object.__setattr__(self, "_binary_identity", identity_copy)
        object.__setattr__(self, "_owned_transport", transport)
        object.__setattr__(self, "_owned_temporary", temporary)
        object.__setattr__(
            self,
            "_identity_snapshot",
            None
            if identity_copy is None
            else _identity_from_record(_identity_record(identity_copy)),
        )
        object.__setattr__(
            self,
            "_issued_state",
            (
                kind,
                prompt,
                id(transport),
                None if identity_copy is None else _identity_record(identity_copy),
            ),
        )
        object.__setattr__(
            self,
            "_temporary_owner_id",
            id(temporary) if temporary is not None else None,
        )
        object.__setattr__(self, "_temporary_cleaned", temporary is None)
        object.__setattr__(self, "_close_lock", threading.RLock())
        object.__setattr__(self, "_closed", False)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("provider launch boundary is read-only")

    @property
    def kind(self) -> TransportKind:
        return self._kind

    @property
    def prompt(self) -> str:
        return self._prompt

    def _validate_issued_state(self) -> None:
        try:
            identity = (
                None
                if self._binary_identity is None
                else _identity_record(self._binary_identity)
            )
            snapshot_identity = (
                None
                if self._identity_snapshot is None
                else _identity_record(self._identity_snapshot)
            )
            current = (
                self._kind,
                self._prompt,
                id(self._transport),
                identity,
            )
        except BaseException:
            raise ConfigurationError(
                "provider launch boundary state changed after issuance"
            ) from None
        if (
            current != self._issued_state
            or snapshot_identity != self._issued_state[3]
            or id(self._owned_transport) != self._issued_state[2]
            or self._transport is not self._owned_transport
        ):
            raise ConfigurationError(
                "provider launch boundary state changed after issuance"
            )
        if self._temporary_cleaned:
            if (
                self._temporary is not None
                or self._owned_temporary is not None
                or self._temporary_owner_id is not None
            ):
                raise ConfigurationError(
                    "provider launch boundary state changed after issuance"
                )
        elif (
            self._temporary is not self._owned_temporary
            or id(self._temporary) != self._temporary_owner_id
        ):
            raise ConfigurationError(
                "provider launch boundary state changed after issuance"
            )

    def _issued_transport_for_cleanup(self) -> Any:
        expected_id = self._issued_state[2]
        if id(self._owned_transport) == expected_id:
            return self._owned_transport
        if id(self._transport) == expected_id:
            return self._transport
        raise ConfigurationError(
            "provider launch boundary owner state changed after issuance"
        )

    def _issued_temporary_for_cleanup(self) -> Any:
        expected_id = self._temporary_owner_id
        if expected_id is None:
            return None
        if id(self._owned_temporary) == expected_id:
            return self._owned_temporary
        if id(self._temporary) == expected_id:
            return self._temporary
        raise ConfigurationError(
            "provider launch boundary owner state changed after issuance"
        )

    def _require_operation(self, kind: TransportKind) -> Any:
        self._validate_issued_state()
        if self._closed:
            raise TransportError("provider launch boundary is closed")
        if self._kind is not kind:
            raise ConfigurationError(
                "operation is not valid for this protocol boundary"
            )
        return self._owned_transport

    def send(self, value: Any) -> None:
        self._require_operation(TransportKind.JSONL).send(value)

    async def send_async(self, value: Any) -> None:
        await self._require_operation(TransportKind.JSONL).send_async(value)

    def receive(self) -> Optional[Any]:
        return self._require_operation(TransportKind.JSONL).receive()

    async def receive_async(self) -> Optional[Any]:
        return await self._require_operation(TransportKind.JSONL).receive_async()

    def iter_messages(self):
        transport = self._require_operation(TransportKind.JSONL)
        for message in transport.iter_messages():
            yield message

    async def aiter_messages(self):
        transport = self._require_operation(TransportKind.JSONL)
        iterator = transport.aiter_messages()
        try:
            async for message in iterator:
                yield message
        finally:
            try:
                await iterator.aclose()
            finally:
                await self.close_async()

    def close_stdin(self) -> None:
        self._require_operation(TransportKind.JSONL).close_stdin()

    def request(self, method: str, params: Any = None) -> Any:
        return self._require_operation(TransportKind.JSON_RPC).request(
            method, params
        )

    async def request_async(self, method: str, params: Any = None) -> Any:
        return await self._require_operation(TransportKind.JSON_RPC).request_async(
            method, params
        )

    async def close_async(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.close)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            failure = None
            try:
                self._validate_issued_state()
            except BaseException as caught:
                failure = caught
            transport_closed = False
            try:
                transport = self._issued_transport_for_cleanup()
                transport.close()
                transport_closed = True
            except BaseException as caught:
                if failure is None:
                    failure = caught

            if not self._temporary_cleaned:
                temporary = None
                try:
                    temporary = self._issued_temporary_for_cleanup()
                    if temporary is not None:
                        _cleanup_temporary(temporary)
                except BaseException as caught:
                    if failure is None:
                        failure = caught
                finally:
                    if temporary is None or temporary.cleaned:
                        object.__setattr__(self, "_temporary", None)
                        object.__setattr__(self, "_owned_temporary", None)
                        object.__setattr__(self, "_temporary_owner_id", None)
                        object.__setattr__(self, "_temporary_cleaned", True)
            identity_verified = self._identity_snapshot is None
            if self._identity_snapshot is not None:
                try:
                    if _identity_record(self._identity_snapshot) != self._issued_state[3]:
                        raise ConfigurationError(
                            "provider launch boundary state changed after issuance"
                        )
                    _identity_from_record(self._issued_state[3]).verify_metadata()
                    identity_verified = True
                except BaseException as caught:
                    if failure is None:
                        failure = caught

            if (
                failure is None
                and transport_closed
                and self._temporary_cleaned
                and identity_verified
            ):
                object.__setattr__(self, "_closed", True)
            if failure is not None:
                raise failure

    def __enter__(self) -> "ProtocolLaunchBoundaryV1":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self.close()
        except BaseException as failure:
            if exc_type is None or (
                isinstance(failure, TransportError) and "reaped" in str(failure)
            ):
                raise

    def __repr__(self) -> str:
        return "<ProtocolLaunchBoundaryV1 kind={} closed={}>".format(
            self._kind.value, self._closed
        )


class InteractiveAuthSessionV1:
    """Single-use, runtime-owned fixed auth command for a REPL TTY.

    The argv, workspace, environment, and executable identity are private
    runtime state.  A caller can choose only which already-open TTY streams
    receive the provider's interactive UI; browser opening is never performed.
    """

    __slots__ = (
        "_argv",
        "_binary_identity",
        "_provider_env",
        "_allowed_provider_env",
        "_provider_home",
        "_limits",
        "_cancellation",
        "_cwd",
        "_temporary",
        "_owned_temporary",
        "_temporary_owner_id",
        "_issued_state",
        "_state_lock",
        "_run_state",
        "_cleanup_pending",
        "_closed",
    )

    def __init__(
        self,
        argv: Tuple[str, ...],
        binary_identity: ExecutableIdentity,
        provider_env: Mapping[str, str],
        allowed_provider_env: Tuple[str, ...],
        provider_home: str,
        limits: OperationLimits,
        cancellation: Optional[CancellationToken],
        cwd: str,
        temporary: Any,
    ) -> None:
        if type(temporary) is not _OwnedTemporaryDirectory:
            raise ConfigurationError("interactive auth temporary owner is invalid")
        argv_copy = tuple(argv)
        identity_copy = _identity_from_record(_identity_record(binary_identity))
        environment_copy = MappingProxyType(dict(provider_env))
        allowed_copy = tuple(allowed_provider_env)
        limits_copy = _limits_from_record(_limits_record(limits))
        object.__setattr__(self, "_argv", argv_copy)
        object.__setattr__(self, "_binary_identity", identity_copy)
        object.__setattr__(self, "_provider_env", environment_copy)
        object.__setattr__(self, "_allowed_provider_env", allowed_copy)
        object.__setattr__(self, "_provider_home", provider_home)
        object.__setattr__(self, "_limits", limits_copy)
        object.__setattr__(self, "_cancellation", cancellation)
        object.__setattr__(self, "_cwd", cwd)
        object.__setattr__(self, "_temporary", temporary)
        object.__setattr__(self, "_owned_temporary", temporary)
        object.__setattr__(self, "_temporary_owner_id", id(temporary))
        state_lock = threading.RLock()
        object.__setattr__(self, "_state_lock", state_lock)
        object.__setattr__(
            self,
            "_issued_state",
            (
                argv_copy,
                _identity_record(identity_copy),
                tuple(environment_copy.items()),
                allowed_copy,
                provider_home,
                _limits_record(limits_copy),
                cancellation,
                cwd,
                id(state_lock),
            ),
        )
        object.__setattr__(self, "_run_state", "unused")
        object.__setattr__(self, "_cleanup_pending", False)
        object.__setattr__(self, "_closed", False)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("interactive auth session is read-only")

    def _validate_issued_state(self) -> None:
        if self._issued_state is None:
            raise ConfigurationError(
                "interactive auth session state changed after issuance"
            )
        try:
            current = (
                tuple(self._argv),
                _identity_record(self._binary_identity),
                tuple(self._provider_env.items()),
                tuple(self._allowed_provider_env),
                self._provider_home,
                _limits_record(self._limits),
                self._cancellation,
                self._cwd,
            )
        except BaseException:
            raise ConfigurationError(
                "interactive auth session state changed after issuance"
            ) from None
        if (
            current != self._issued_state[:-1]
            or self._cancellation is not self._issued_state[6]
            or id(self._owned_temporary) != self._temporary_owner_id
            or self._temporary is not self._owned_temporary
            or id(self._state_lock) != self._issued_state[8]
        ):
            raise ConfigurationError(
                "interactive auth session state changed after issuance"
            )

    def _cleanup_owned(self) -> None:
        if self._closed:
            return
        expected_id = self._temporary_owner_id
        if expected_id is not None and id(self._owned_temporary) == expected_id:
            temporary = self._owned_temporary
        elif expected_id is not None and id(self._temporary) == expected_id:
            temporary = self._temporary
        else:
            raise ConfigurationError(
                "interactive auth session owner state changed after issuance"
            )
        try:
            _cleanup_temporary(temporary)
        except BaseException:
            if not temporary.cleaned:
                # Once cleanup starts this handle can never execute.  Retain
                # only the identity-bound owner; discard duplicate workspace
                # pathname state before exposing a retryable failure.
                object.__setattr__(self, "_run_state", "used")
                object.__setattr__(self, "_cwd", None)
                object.__setattr__(self, "_issued_state", None)
                object.__setattr__(self, "_cleanup_pending", True)
            raise
        finally:
            if temporary.cleaned:
                # Do not retain a pathname token after cleanup.  A later
                # directory recreated at the same random name is not ours.
                object.__setattr__(self, "_temporary", None)
                object.__setattr__(self, "_owned_temporary", None)
                object.__setattr__(self, "_temporary_owner_id", None)
                object.__setattr__(self, "_cwd", None)
                object.__setattr__(self, "_issued_state", None)
                object.__setattr__(self, "_cleanup_pending", False)
                object.__setattr__(self, "_closed", True)

    def run(
        self,
        *,
        stdin: Optional[object] = None,
        stdout: Optional[object] = None,
        stderr: Optional[object] = None,
    ) -> int:
        with self._state_lock:
            if self._closed or self._cleanup_pending:
                raise ConfigurationError("interactive auth session is already used")
            try:
                self._validate_issued_state()
            except BaseException:
                try:
                    self._cleanup_owned()
                except BaseException:
                    pass
                raise
            if self._run_state != "unused" or self._closed:
                if not self._closed:
                    self._cleanup_owned()
                raise ConfigurationError("interactive auth session is already used")
            object.__setattr__(self, "_run_state", "running")
            identity = _identity_from_record(self._issued_state[1])
            limits = _limits_from_record(self._issued_state[5])
            try:
                result = _run_interactive_process(
                    self._issued_state[0],
                    timeout=limits.timeout_seconds,
                    cwd=self._issued_state[7],
                    provider_env=dict(self._issued_state[2]),
                    allowed_provider_env=self._issued_state[3],
                    persistent_home=self._issued_state[4],
                    cancellation=self._issued_state[6],
                    executable_identity=identity,
                    stdin=sys.stdin if stdin is None else stdin,
                    stdout=sys.stdout if stdout is None else stdout,
                    stderr=sys.stderr if stderr is None else stderr,
                )
            except BaseException:
                object.__setattr__(self, "_run_state", "used")
                try:
                    self.close()
                except BaseException:
                    pass
                raise
            else:
                object.__setattr__(self, "_run_state", "used")
                self.close()
                return result

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            failure = None
            if not self._cleanup_pending:
                try:
                    self._validate_issued_state()
                except BaseException as caught:
                    failure = caught
            try:
                self._cleanup_owned()
            except BaseException as caught:
                if failure is None:
                    failure = caught
            if failure is not None:
                raise failure

    def __enter__(self) -> "InteractiveAuthSessionV1":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self.close()
        except BaseException:
            if exc_type is None:
                raise

    def __repr__(self) -> str:
        return "<InteractiveAuthSessionV1 used={} closed={}>".format(
            self._run_state != "unused", self._closed
        )


def _model_id(value: object) -> str:
    if type(value) is not str or not value or len(value) > _MAX_MODEL_ID_CHARS:
        raise ProtocolError("model probe returned an invalid model id")
    if value != value.strip():
        raise ProtocolError("model probe returned an invalid model id")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        raise ProtocolError("model probe returned an invalid model id") from None
    if any(
        unicodedata.category(char).startswith("C")
        or unicodedata.category(char) in {"Zl", "Zp"}
        for char in value
    ):
        raise ProtocolError("model probe returned an invalid model id")
    return value


def _clone_adapter_value(value: Any) -> Any:
    """Reconstruct contract values so callers never share policy-bearing state."""

    if is_dataclass(value) and not isinstance(value, type):
        return type(value)(
            **{
                item.name: _clone_adapter_value(getattr(value, item.name))
                for item in fields(value)
                if item.init
            }
        )
    if isinstance(value, Mapping):
        return {
            _clone_adapter_value(key): _clone_adapter_value(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_clone_adapter_value(item) for item in value)
    if isinstance(value, frozenset):
        return frozenset(_clone_adapter_value(item) for item in value)
    if isinstance(value, list):
        return [_clone_adapter_value(item) for item in value]
    return value


class ProviderAdapterV1:
    """Lazy adapter facade over one validated immutable specification."""

    __slots__ = (
        "_spec",
        "_inspection_record",
    )

    def __init__(self, spec: ProviderAdapterSpecV1) -> None:
        if not isinstance(spec, ProviderAdapterSpecV1):
            raise ConfigurationError("adapter must use ProviderAdapterSpecV1")
        # Clone once at issuance so later mutation of the caller's graph cannot
        # alter policy.  Contract objects recursively freeze their collections;
        # ordinary assignment is blocked below.  Hostile object.__setattr__ code
        # already executing in this process is intentionally outside the object
        # security boundary, so hot paths share this canonical immutable graph.
        object.__setattr__(self, "_spec", _clone_adapter_value(spec))
        object.__setattr__(self, "_inspection_record", None)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("provider adapter facade is read-only")

    def _validated_spec(self) -> ProviderAdapterSpecV1:
        if type(self._spec) is not ProviderAdapterSpecV1:
            raise ConfigurationError("provider adapter state changed after issuance")
        return self._spec

    @property
    def spec(self) -> ProviderAdapterSpecV1:
        return self._validated_spec()

    @property
    def descriptor(self) -> AdapterDescriptorV1:
        return describe_adapter(self.spec)

    def resolve_binary(self, path: str) -> BinaryProvenance:
        """Fingerprint one explicitly supplied path without executing it."""

        return _regular_binary(path, self.spec.binary.executable)

    def _ensure_usable(self) -> None:
        if self.spec.status is AdapterStatus.HELD:
            raise ConfigurationError("provider adapter is held and cannot execute")

    def _require_inspection(
        self, inspection: AdapterInspectionV1
    ) -> BinaryProvenance:
        """Require the exact inspection object issued by this adapter."""

        self._ensure_usable()
        issued_record = self._inspection_record
        if (
            type(inspection) is not AdapterInspectionV1
            or issued_record is None
            or issued_record[0] is not inspection
            or type(inspection.id) is not str
            or type(inspection.version) is not str
            or type(inspection.features) is not frozenset
            or type(inspection.binary) is not BinaryProvenance
            or type(inspection.abi_version) is not int
            or inspection.id != issued_record[1]
            or inspection.version != issued_record[2]
            or inspection.features != issued_record[3]
            or inspection.binary is not issued_record[4]
            or inspection.binary != issued_record[5]
            or inspection.abi_version != issued_record[6]
            or inspection.id != self.spec.id
            or inspection.abi_version != self.spec.abi_version
        ):
            raise ConfigurationError("adapter inspection does not match this adapter")
        inspection.binary.verify(self.spec.binary.executable)
        return inspection.binary

    def _selected_environment(
        self, provider_env: Optional[Mapping[str, str]]
    ) -> Mapping[str, str]:
        return self.spec.environment.select(provider_env)

    @staticmethod
    def _plain_record(text: str, probe: PlainTextProbeSpec) -> Mapping[str, Any]:
        lines = tuple(
            line[:-1] if line.endswith("\r") else line
            for line in text.split("\n")
        )
        for marker in probe.required_markers:
            if marker not in lines:
                raise ProtocolError("provider plain-text probe is missing a required marker")
        result = {}
        for name, field_spec in probe.fields.items():
            if field_spec.presence_only:
                matches = [line for line in lines if line == field_spec.marker]
            else:
                matches = [line for line in lines if line.startswith(field_spec.marker)]
            if not matches:
                continue
            if len(matches) != 1:
                raise ProtocolError("provider plain-text probe field is ambiguous")
            if field_spec.presence_only:
                result[name] = True
                continue
            line = matches[0]
            value = line[len(field_spec.marker) :]
            if field_spec.terminator in (None, "\n", "\r\n"):
                end = len(value)
            else:
                if "\n" in field_spec.terminator or "\r" in field_spec.terminator:
                    raise ProtocolError(
                        "provider plain-text probe terminator must stay on one line"
                    )
                end = value.find(field_spec.terminator)
                if end < 0:
                    raise ProtocolError("provider plain-text probe field is unterminated")
            value = value[:end]
            if len(value) > field_spec.max_chars:
                raise ProtocolError("provider plain-text probe field exceeds its limit")
            result[name] = value
        return MappingProxyType(result)

    @staticmethod
    def _plain_identity_matches(
        text: str,
        probe: PlainTextProbeSpec,
        expected_identity: str,
    ) -> bool:
        lines = tuple(
            line[:-1] if line.endswith("\r") else line
            for line in text.split("\n")
        )
        marker = probe.identity_marker or expected_identity
        identity_lines = lines.count(marker)
        if identity_lines == 1:
            return True
        if identity_lines > 1:
            return False
        if probe.identity_marker is not None:
            return False
        # A version line may combine identity and value.  It is accepted only
        # when the declarative field marker is anchored, unique, and explicitly
        # begins with the complete identity followed by a separator.
        for field_spec in probe.fields.values():
            if field_spec.presence_only or not field_spec.marker.startswith(
                expected_identity + " "
            ):
                continue
            if sum(line.startswith(field_spec.marker) for line in lines) == 1:
                return True
        return False

    def _run_probe(
        self,
        binary: BinaryProvenance,
        probe: DeclarativeProbeSpec,
        *,
        provider_env: Optional[Mapping[str, str]] = None,
        provider_home: Optional[str] = None,
        cancellation: Optional[CancellationToken] = None,
    ) -> Mapping[str, Any]:
        self._ensure_usable()
        binary.verify(self.spec.binary.executable)
        selected = self._selected_environment(provider_env)
        _reserve_pending_cleanup_slot()
        temporary = None
        process = None
        try:
            temporary = _OwnedTemporaryDirectory(prefix="unified-cli-ext-probe-")
            temporary.create()
            root = temporary.name
            workspace = os.path.join(root, "workspace")
            os.mkdir(workspace, 0o700)
            workspace = os.path.realpath(workspace)
            if isinstance(probe, JsonProbeSpec) and probe.format is ProbeFormat.JSONL:
                process = JsonlProcess(
                    probe.command.build(binary.real_path),
                    timeout=probe.command.limits.timeout_seconds,
                    cwd=workspace,
                    provider_env=selected,
                    allowed_provider_env=tuple(self.spec.environment.allowed_keys),
                    persistent_home=provider_home,
                    limits=probe.command.limits.transport_limits(),
                    cancellation=cancellation,
                    executable_identity=binary.executable_identity(),
                )
                messages = []
                with process:
                    for message in process.iter_messages():
                        messages.append(message)
                        if len(messages) > 1:
                            raise ProtocolError("provider probe returned multiple records")
                if len(messages) != 1:
                    raise ProtocolError("provider probe did not return one record")
                result = messages[0]
            else:
                fixed = run_fixed_process(
                    probe.command.build(binary.real_path),
                    timeout=probe.command.limits.timeout_seconds,
                    cwd=workspace,
                    provider_env=selected,
                    allowed_provider_env=tuple(self.spec.environment.allowed_keys),
                    persistent_home=provider_home,
                    limits=probe.command.limits.transport_limits(),
                    cancellation=cancellation,
                    executable_identity=binary.executable_identity(),
                )
                if isinstance(probe, ExitStatusProbeSpec):
                    if fixed.returncode != probe.expected_status:
                        raise ProcessFailed(fixed.returncode, fixed.stderr)
                    result = MappingProxyType({"exit_status": fixed.returncode})
                else:
                    if fixed.returncode != 0:
                        raise ProcessFailed(fixed.returncode, fixed.stderr)
                    if isinstance(probe, JsonProbeSpec):
                        try:
                            value = strict_json_loads(fixed.stdout)
                        except (TypeError, ValueError, UnicodeError, RecursionError):
                            raise ProtocolError("provider probe returned malformed JSON") from None
                        if type(value) is not dict:
                            raise ProtocolError("provider JSON probe must return one object")
                        try:
                            bounded = freeze_json(value, drop_reasoning=False)
                        except ProtocolError:
                            raise ProtocolError("provider JSON probe is not bounded JSON") from None
                        result = MappingProxyType(dict(bounded))
                    else:
                        result = self._plain_record(fixed.stdout, probe)
            binary.verify(self.spec.binary.executable)
            if isinstance(probe, JsonProbeSpec):
                if (
                    probe.identity_field not in result
                    or result[probe.identity_field]
                    != self.spec.binary.expected_identity
                ):
                    raise ProtocolError(
                        "provider binary identity did not match adapter metadata"
                    )
                expected_items = probe.expected.items()
            elif isinstance(probe, PlainTextProbeSpec):
                if not self._plain_identity_matches(
                    fixed.stdout,
                    probe,
                    self.spec.binary.expected_identity,
                ):
                    raise ProtocolError(
                        "provider binary identity did not match adapter metadata"
                    )
                expected_items = probe.expected.items()
            else:
                expected_items = ()
            for key, expected in expected_items:
                # ``None`` means presence-only.  It must not silently accept a
                # missing key through ``mapping.get``.
                if key not in result or (
                    expected is not None and result[key] != expected
                ):
                    raise ProtocolError(
                        "provider probe result did not match its specification"
                    )
            final_result = MappingProxyType(dict(result))
        except BaseException as failure:
            owners = (() if process is None else (process,)) + (
                () if temporary is None else (temporary,)
            )
            _cleanup_or_retain(owners, failure, reserved=True)
            raise
        assert temporary is not None
        _cleanup_or_retain((temporary,), reserved=True)
        return final_result

    def inspect(
        self,
        binary: BinaryProvenance,
        *,
        provider_env: Optional[Mapping[str, str]] = None,
        provider_home: Optional[str] = None,
        cancellation: Optional[CancellationToken] = None,
    ) -> AdapterInspectionV1:
        """Run the explicit version and feature probes for one binary."""

        version_spec = self.spec.binary.version_probe
        if version_spec.format is ProbeFormat.PLAIN_TEXT:
            version_probe = PlainTextProbeSpec(
                version_spec.command,
                fields={
                    version_spec.version_field: PlainTextFieldSpec(
                        version_spec.version_marker,
                        max_chars=128,
                    )
                },
                expected={version_spec.version_field: None},
                identity_marker=version_spec.identity_marker,
            )
        else:
            version_probe = JsonProbeSpec(
                version_spec.command,
                identity_field=version_spec.identity_field,
                format=version_spec.format,
            )
        version_record = self._run_probe(
            binary,
            version_probe,
            provider_env=provider_env,
            provider_home=provider_home,
            cancellation=cancellation,
        )
        version = version_record.get(version_spec.version_field)
        if type(version) is not str or len(version) > 128:
            raise ProtocolError("provider version probe returned an invalid version")
        match = _VERSION_RE.fullmatch(version)
        if match is None:
            raise ProtocolError("provider version probe returned an invalid version")
        release = tuple(int(item) for item in match.group("release").split("."))
        if any(item > 1_000_000 for item in release):
            raise ProtocolError("provider version probe returned an invalid version")
        prerelease = match.group("prerelease")
        if prerelease is not None and any(
            len(item) > 1 and item.isdigit() and item.startswith("0")
            for item in prerelease[1:].split(".")
        ):
            raise ProtocolError("provider version probe returned an invalid version")
        width = max(len(release), len(version_spec.minimum_version))
        padded_release = release + (0,) * (width - len(release))
        padded_minimum = version_spec.minimum_version + (0,) * (
            width - len(version_spec.minimum_version)
        )
        if padded_release < padded_minimum:
            raise ProtocolError("provider binary version is below the adapter minimum")
        if padded_release == padded_minimum and prerelease is not None:
            raise ProtocolError("provider prerelease does not satisfy the final minimum")

        feature_spec = self.spec.binary.feature_probe
        if feature_spec.format is ProbeFormat.PLAIN_TEXT:
            feature_probe = PlainTextProbeSpec(
                feature_spec.command,
                required_markers=tuple(
                    feature_spec.feature_markers[name]
                    for name in sorted(feature_spec.required_features)
                ),
                fields={
                    name: PlainTextFieldSpec(marker, presence_only=True)
                    for name, marker in feature_spec.feature_markers.items()
                },
                identity_marker=feature_spec.identity_marker,
            )
        else:
            feature_probe = JsonProbeSpec(
                feature_spec.command,
                identity_field=feature_spec.identity_field,
                format=feature_spec.format,
            )
        feature_record = self._run_probe(
            binary,
            feature_probe,
            provider_env=provider_env,
            provider_home=provider_home,
            cancellation=cancellation,
        )
        if feature_spec.format is ProbeFormat.PLAIN_TEXT:
            raw_features = tuple(
                name for name in feature_spec.feature_markers if name in feature_record
            )
        else:
            raw_features = feature_record.get(feature_spec.features_field)
        if not isinstance(raw_features, (list, tuple)) or len(raw_features) > 128:
            raise ProtocolError("provider feature probe returned an invalid feature set")
        features = []
        for value in raw_features:
            if (
                type(value) is not str
                or len(value) > 64
                or _FEATURE_RE.fullmatch(value) is None
            ):
                raise ProtocolError("provider feature probe returned an invalid feature set")
            features.append(value)
        if len(features) != len(set(features)):
            raise ProtocolError("provider feature probe returned duplicate features")
        frozen_features = frozenset(features)
        if not feature_spec.required_features <= frozen_features:
            raise ProtocolError("provider binary is missing required adapter features")
        inspection = AdapterInspectionV1(
            id=self.spec.id,
            version=version,
            features=frozen_features,
            binary=binary,
            abi_version=self.spec.abi_version,
        )
        object.__setattr__(self, "_inspection_record", (
            inspection,
            inspection.id,
            inspection.version,
            inspection.features,
            inspection.binary,
            BinaryProvenance(
                invoked_path=inspection.binary.invoked_path,
                real_path=inspection.binary.real_path,
                sha256=inspection.binary.sha256,
                device=inspection.binary.device,
                inode=inspection.binary.inode,
                size=inspection.binary.size,
                mtime_ns=inspection.binary.mtime_ns,
                mode=inspection.binary.mode,
                owner=inspection.binary.owner,
                parent_chain=inspection.binary.parent_chain,
                interpreter=(
                    None
                    if inspection.binary.interpreter is None
                    else _identity_from_record(
                        _identity_record(inspection.binary.interpreter)
                    )
                ),
                ctime_ns=inspection.binary.ctime_ns,
            ),
            inspection.abi_version,
        ))
        return inspection

    def doctor_provider(
        self,
        inspection: AdapterInspectionV1,
        *,
        provider_env: Optional[Mapping[str, str]] = None,
        provider_home: Optional[str] = None,
        cancellation: Optional[CancellationToken] = None,
    ) -> bool:
        binary = self._require_inspection(inspection)
        if self.spec.doctor is None:
            raise ConfigurationError("provider adapter has no doctor probe")
        result = self._run_probe(
            binary,
            self.spec.doctor.probe,
            provider_env=provider_env,
            provider_home=provider_home,
            cancellation=cancellation,
        )
        if isinstance(self.spec.doctor.probe, ExitStatusProbeSpec):
            return True
        value = result.get(self.spec.doctor.healthy_field)
        if type(value) is not bool:
            raise ProtocolError("provider doctor returned an invalid status")
        return value

    def list_models(
        self,
        inspection: AdapterInspectionV1,
        *,
        provider_env: Optional[Mapping[str, str]] = None,
        provider_home: Optional[str] = None,
        cancellation: Optional[CancellationToken] = None,
    ) -> Tuple[str, ...]:
        binary = self._require_inspection(inspection)
        if self.spec.models is None:
            raise ConfigurationError("provider adapter has no model probe")
        result = self._run_probe(
            binary,
            self.spec.models.probe,
            provider_env=provider_env,
            provider_home=provider_home,
            cancellation=cancellation,
        )
        values = result.get(self.spec.models.models_field)
        if not isinstance(values, (list, tuple)) or len(values) > self.spec.models.max_models:
            raise ProtocolError("provider model probe returned an invalid model list")
        models = tuple(_model_id(value) for value in values)
        if len(models) != len(set(models)):
            raise ProtocolError("provider model probe returned duplicate model ids")
        return models

    def authenticated(
        self,
        inspection: AdapterInspectionV1,
        *,
        provider_env: Optional[Mapping[str, str]] = None,
        provider_home: Optional[str] = None,
        cancellation: Optional[CancellationToken] = None,
    ) -> bool:
        binary = self._require_inspection(inspection)
        if self.spec.auth is None:
            raise ConfigurationError("provider adapter has no authentication specification")
        result = self._run_probe(
            binary,
            self.spec.auth.status_probe,
            provider_env=provider_env,
            provider_home=provider_home,
            cancellation=cancellation,
        )
        if isinstance(self.spec.auth.status_probe, ExitStatusProbeSpec):
            return True
        value = result.get(self.spec.auth.authenticated_field)
        if type(value) is not bool:
            raise ProtocolError("provider auth probe returned an invalid status")
        return value

    def build_auth_login(self, binary: BinaryProvenance) -> Tuple[str, ...]:
        raise ConfigurationError(
            "build_auth_login is deprecated; use prepare_auth_login for runtime-owned execution"
        )

    def _prepare_auth_command(
        self,
        inspection: AdapterInspectionV1,
        command: FixedCommandSpec,
        *,
        provider_home: str,
        provider_env: Optional[Mapping[str, str]] = None,
        cancellation: Optional[CancellationToken] = None,
    ) -> InteractiveAuthSessionV1:
        binary = self._require_inspection(inspection)
        selected = self._selected_environment(provider_env)
        home = private_persistent_home(provider_home)
        _reserve_pending_cleanup_slot()
        temporary = None
        try:
            temporary = _OwnedTemporaryDirectory(prefix="unified-cli-ext-auth-")
            temporary.create()
            workspace = os.path.join(temporary.name, "workspace")
            os.mkdir(workspace, 0o700)
            workspace = os.path.realpath(workspace)
            workspace = validated_workspace(workspace)
            session = InteractiveAuthSessionV1(
                command.build(binary.real_path),
                binary.executable_identity(),
                selected,
                tuple(self.spec.environment.allowed_keys),
                home,
                command.limits,
                cancellation,
                workspace,
                temporary,
            )
        except BaseException as failure:
            owners = () if temporary is None else (temporary,)
            _cleanup_or_retain(owners, failure, reserved=True)
            raise
        _release_pending_cleanup_slot()
        return session

    def prepare_auth_login(
        self,
        inspection: AdapterInspectionV1,
        *,
        provider_home: str,
        provider_env: Optional[Mapping[str, str]] = None,
        cancellation: Optional[CancellationToken] = None,
    ) -> InteractiveAuthSessionV1:
        if self.spec.auth is None:
            raise ConfigurationError("provider adapter has no authentication specification")
        return self._prepare_auth_command(
            inspection,
            self.spec.auth.login_command,
            provider_home=provider_home,
            provider_env=provider_env,
            cancellation=cancellation,
        )

    def prepare_auth_logout(
        self,
        inspection: AdapterInspectionV1,
        *,
        provider_home: str,
        provider_env: Optional[Mapping[str, str]] = None,
        cancellation: Optional[CancellationToken] = None,
    ) -> InteractiveAuthSessionV1:
        if self.spec.auth is None or self.spec.auth.logout_command is None:
            raise ConfigurationError("provider adapter has no logout command")
        return self._prepare_auth_command(
            inspection,
            self.spec.auth.logout_command,
            provider_home=provider_home,
            provider_env=provider_env,
            cancellation=cancellation,
        )

    def execute_auth_login(
        self,
        inspection: AdapterInspectionV1,
        *,
        provider_home: str,
        provider_env: Optional[Mapping[str, str]] = None,
        cancellation: Optional[CancellationToken] = None,
        stdin: Optional[object] = None,
        stdout: Optional[object] = None,
        stderr: Optional[object] = None,
    ) -> int:
        """Run the fixed login command on TTY streams without opening a browser."""

        return self.prepare_auth_login(
            inspection,
            provider_home=provider_home,
            provider_env=provider_env,
            cancellation=cancellation,
        ).run(stdin=stdin, stdout=stdout, stderr=stderr)

    def execute_auth_logout(
        self,
        inspection: AdapterInspectionV1,
        *,
        provider_home: str,
        provider_env: Optional[Mapping[str, str]] = None,
        cancellation: Optional[CancellationToken] = None,
        stdin: Optional[object] = None,
        stdout: Optional[object] = None,
        stderr: Optional[object] = None,
    ) -> int:
        return self.prepare_auth_logout(
            inspection,
            provider_home=provider_home,
            provider_env=provider_env,
            cancellation=cancellation,
        ).run(stdin=stdin, stdout=stdout, stderr=stderr)

    def build_prompt(
        self,
        binary: BinaryProvenance,
        prompt: str,
        values: Optional[Mapping[str, str]] = None,
    ) -> BuiltPromptInvocation:
        self._ensure_usable()
        binary.verify(self.spec.binary.executable)
        return self.spec.prompt.build(binary.real_path, prompt, values)

    def open_transport(
        self,
        inspection: AdapterInspectionV1,
        prompt: str,
        values: Optional[Mapping[str, str]] = None,
        *,
        cwd: Optional[str] = None,
        provider_env: Optional[Mapping[str, str]] = None,
        provider_home: Optional[str] = None,
        cancellation: Optional[CancellationToken] = None,
    ) -> Any:
        """Dispatch from declared transport metadata to a safe concrete boundary."""

        binary = self._require_inspection(inspection)
        workspace = validated_workspace(cwd)
        kind = self.spec.transport
        if kind is TransportKind.ACP:
            raise ConfigurationError(
                "ACP provider execution is not implemented: ABI v1 has no verified SDK stdio lifecycle"
            )
        if kind in (TransportKind.HTTP_JSON, TransportKind.HTTP_SSE):
            raise ConfigurationError(
                "HTTP provider execution is not implemented: ABI v1 has no provider-owned daemon lifecycle"
            )
        invocation = self.spec.prompt.build(binary.real_path, prompt, values)
        selected = self._selected_environment(provider_env)
        allowed = tuple(self.spec.environment.allowed_keys)
        identity = binary.executable_identity()

        if kind in (TransportKind.PLAIN, TransportKind.JSON):
            return OpenedProcessTransportV1(
                kind,
                invocation,
                identity,
                selected,
                allowed,
                provider_home,
                self.spec.prompt.limits,
                cancellation,
                workspace,
            )

        if kind in (TransportKind.JSONL, TransportKind.JSON_RPC):
            common = dict(
                timeout=self.spec.prompt.limits.timeout_seconds,
                cwd=workspace,
                provider_env=selected,
                allowed_provider_env=allowed,
                persistent_home=provider_home,
                limits=self.spec.prompt.limits.transport_limits(),
                cancellation=cancellation,
                executable_identity=identity,
            )
            if kind is TransportKind.JSONL:
                transport = JsonlProcess(invocation.argv, **common)
            else:
                transport = JsonRpcProcessClient(invocation.argv, **common)
            try:
                return ProtocolLaunchBoundaryV1(
                    kind,
                    transport,
                    invocation.protocol_text or "",
                    None,
                    identity,
                )
            except BaseException as failure:
                _cleanup_or_retain((transport,), failure)
                raise
        raise ConfigurationError("unsupported provider transport")

    def session_ref(self, session_id: str) -> SessionRef:
        return SessionRef(self.spec.session_namespace, session_id)

    def require_server_access(self) -> None:
        """Fail closed even if a caller mistakes metadata for authorization."""

        raise ConfigurationError("provider adapters are disabled in server mode")

__all__ = [
    "AdapterInspectionV1",
    "BinaryProvenance",
    "InteractiveAuthSessionV1",
    "OpenedProcessTransportV1",
    "ProtocolLaunchBoundaryV1",
    "ProviderAdapterV1",
    "drain_pending_cleanups",
]
