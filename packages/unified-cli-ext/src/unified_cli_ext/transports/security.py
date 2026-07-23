"""Environment isolation, cancellation, and diagnostic redaction."""

from __future__ import annotations

import hashlib
import os
import json
import math
import re
import secrets
import shutil
import stat
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from ..errors import ConfigurationError, TransportCancelled


_BASE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM")
_SECRET_PATTERN = re.compile(
    r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|api[_-]?key\s*[:=]\s*|token\s*[:=]\s*|password\s*[:=]\s*)[^\s,;]+"
)
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
_MAX_SHEBANG_BYTES = 4096
_MAX_DIAGNOSTIC_SECRET_CHARS = 64 * 1024
_MAX_DIAGNOSTIC_SECRET_BYTES = 256 * 1024
_MAX_DIAGNOSTIC_MATCHES = 8192
_MAX_INTERPRETER_DEPTH = 4


def _executable_parent_chain(
    executable_path: str,
) -> Tuple[Tuple[str, int, int, int, int], ...]:
    """Capture the canonical executable parent's authority-bearing chain."""

    if not hasattr(os, "geteuid"):
        raise ConfigurationError("executable ownership could not be verified")
    effective_uid = os.geteuid()
    parent = os.path.dirname(executable_path)
    current = os.path.sep
    candidates = [current]
    for component in (item for item in parent.split(os.path.sep) if item):
        current = os.path.join(current, component)
        candidates.append(current)
    records = []
    for candidate in candidates:
        try:
            metadata = os.lstat(candidate)
        except OSError:
            raise ConfigurationError(
                "executable parent chain could not be inspected"
            ) from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ConfigurationError(
                "executable parent chain must contain only directories"
            )
        mode = stat.S_IMODE(metadata.st_mode)
        if metadata.st_uid not in (0, effective_uid):
            raise ConfigurationError("executable parent has an unsafe owner")
        if mode & (stat.S_IWGRP | stat.S_IWOTH) and not (
            metadata.st_mode & stat.S_ISVTX
        ):
            raise ConfigurationError("executable parent has unsafe permissions")
        records.append(
            (
                candidate,
                int(metadata.st_dev),
                int(metadata.st_ino),
                int(metadata.st_uid),
                mode,
            )
        )
    return tuple(records)


class CancellationToken:
    """Thread-safe explicit cancellation signal shared by sync/async APIs."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise TransportCancelled("extension operation cancelled")


@dataclass(frozen=True)
class TransportLimits:
    max_line_bytes: int = 1024 * 1024
    max_output_bytes: int = 16 * 1024 * 1024
    max_stderr_bytes: int = 1024 * 1024
    max_events: int = 50_000
    max_body_bytes: int = 16 * 1024 * 1024
    max_redirects: int = 3

    def __post_init__(self) -> None:
        for value in (
            self.max_line_bytes,
            self.max_output_bytes,
            self.max_stderr_bytes,
            self.max_events,
            self.max_body_bytes,
        ):
            if type(value) is not int or value <= 0:
                raise ValueError("transport limits must be positive integers")
        if type(self.max_redirects) is not int or not 0 <= self.max_redirects <= 10:
            raise ValueError("max_redirects must be an integer between zero and ten")


@dataclass(frozen=True)
class ExecutableIdentity:
    """A local file-identity guard, not proof of official provenance.

    The guard is freshly opened, ``fstat``-checked, and hashed immediately
    before ``Popen``.  There remains a narrow portable-POSIX pathname race:
    another process with the same user's directory-write authority can rename
    a different executable over the path between this check and ``execve``.
    Executing an already-open descriptor would close that race for native
    binaries, but is not portable for shebang scripts and package-manager
    shims.  Adapter installation/acquisition receipts therefore remain a
    separate provider-layer trust requirement.
    """

    path: str
    sha256: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    mode: int
    owner: int = -1
    parent_chain: Tuple[Tuple[str, int, int, int, int], ...] = ()
    interpreter: Optional["ExecutableIdentity"] = None
    ctime_ns: int = -1

    @classmethod
    def capture(cls, path: str) -> "ExecutableIdentity":
        return cls._capture(path, ())

    @classmethod
    def _capture(
        cls, path: str, ancestor_paths: Tuple[str, ...]
    ) -> "ExecutableIdentity":
        if len(ancestor_paths) > _MAX_INTERPRETER_DEPTH:
            raise ConfigurationError("executable interpreter chain exceeds maximum depth")
        if type(path) is not str or not path or "\x00" in path or not os.path.isabs(path):
            raise ConfigurationError("executable identity path is invalid")
        real_path = os.path.normpath(path)
        if len(os.fsencode(real_path)) > 16 * 1024:
            raise ConfigurationError("executable identity path is invalid")
        if os.path.realpath(real_path) != real_path:
            raise ConfigurationError("executable path must be canonical and non-symlinked")
        if real_path in ancestor_paths:
            raise ConfigurationError("executable interpreter chain is recursive")
        parent_chain = _executable_parent_chain(real_path)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(real_path, flags)
        except OSError:
            raise ConfigurationError("executable could not be opened safely") from None
        digest = hashlib.sha256()
        executable_header = bytearray()
        interpreter_identity = None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ConfigurationError("executable must resolve to a regular file")
            effective_uid = os.geteuid()
            if metadata.st_uid not in (0, effective_uid):
                raise ConfigurationError("executable has an unsafe owner")
            if metadata.st_size < 0 or metadata.st_size > _MAX_EXECUTABLE_BYTES:
                raise ConfigurationError("executable size is outside the allowed range")
            if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise ConfigurationError("executable must not be group/world writable")
            if not metadata.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                raise ConfigurationError("executable is not executable")
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                if len(executable_header) < _MAX_SHEBANG_BYTES:
                    remaining = _MAX_SHEBANG_BYTES - len(executable_header)
                    executable_header.extend(chunk[:remaining])
                digest.update(chunk)
            if executable_header.startswith(b"#!"):
                first_line = bytes(executable_header).split(b"\n", 1)[0]
                if len(first_line) >= _MAX_SHEBANG_BYTES:
                    raise ConfigurationError("executable shebang is invalid")
                shebang = first_line[2:].lstrip()
                interpreter = shebang.split(None, 1)[0] if shebang else b""
                if (
                    not interpreter.startswith(b"/")
                    or os.path.basename(os.fsdecode(interpreter)) == "env"
                ):
                    # ``/usr/bin/env node`` and similar shims reintroduce PATH
                    # lookup after argv[0] was verified.  ABI v1 deliberately
                    # rejects that dispatch.  A future adapter may instead bind
                    # a separately fingerprinted interpreter and script receipt.
                    raise ConfigurationError(
                        "executable shebang must bind an absolute interpreter without env"
                    )
                try:
                    interpreter_path = os.fsdecode(interpreter)
                except UnicodeError:
                    raise ConfigurationError("executable shebang is invalid") from None
                interpreter_identity = cls._capture(
                    interpreter_path, ancestor_paths + (real_path,)
                )
            metadata_after = os.fstat(descriptor)
            path_metadata = os.lstat(real_path)
            parent_chain_after = _executable_parent_chain(real_path)
            identity_fields = (
                "st_dev",
                "st_ino",
                "st_uid",
                "st_mode",
                "st_size",
            )
            if (
                any(
                    getattr(metadata_after, name) != getattr(metadata, name)
                    for name in identity_fields
                )
                or getattr(metadata_after, "st_mtime_ns", int(metadata_after.st_mtime * 1e9))
                != getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1e9))
                or getattr(metadata_after, "st_ctime_ns", int(metadata_after.st_ctime * 1e9))
                != getattr(metadata, "st_ctime_ns", int(metadata.st_ctime * 1e9))
                or any(
                    getattr(path_metadata, name) != getattr(metadata, name)
                    for name in identity_fields
                )
                or getattr(path_metadata, "st_mtime_ns", int(path_metadata.st_mtime * 1e9))
                != getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1e9))
                or getattr(path_metadata, "st_ctime_ns", int(path_metadata.st_ctime * 1e9))
                != getattr(metadata, "st_ctime_ns", int(metadata.st_ctime * 1e9))
                or parent_chain_after != parent_chain
            ):
                raise ConfigurationError("executable changed while it was fingerprinted")
        except OSError:
            raise ConfigurationError("executable could not be fingerprinted") from None
        finally:
            os.close(descriptor)
        return cls(
            path=real_path,
            sha256=digest.hexdigest(),
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size=metadata.st_size,
            mtime_ns=getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1e9)),
            mode=stat.S_IMODE(metadata.st_mode),
            owner=metadata.st_uid,
            parent_chain=parent_chain,
            interpreter=interpreter_identity,
            ctime_ns=getattr(metadata, "st_ctime_ns", int(metadata.st_ctime * 1e9)),
        )

    def verify(self) -> None:
        """Verify captured identity metadata without rereading executable content.

        The SHA-256 digest is established once by :meth:`capture`.  Subsequent
        spawn checks bind the canonical path, complete file metadata, parent
        authority chain, and recursively captured interpreter metadata.  An
        adapter's installation/acquisition receipt remains the provenance
        boundary; repeated local hashing would add latency without closing the
        documented portable pathname race.
        """

        self.verify_metadata()

    def verify_metadata(self) -> None:
        """Check path and metadata captured with the authoritative digest."""

        self._verify_metadata(0)

    def _verify_metadata(self, depth: int) -> None:
        if depth > _MAX_INTERPRETER_DEPTH:
            raise ConfigurationError("executable interpreter chain exceeds maximum depth")

        if os.path.realpath(self.path) != self.path:
            raise ConfigurationError("provider binary identity changed during execution")
        parent_chain = _executable_parent_chain(self.path)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags)
        except OSError:
            raise ConfigurationError("provider binary identity changed during execution") from None
        try:
            metadata = os.fstat(descriptor)
            path_metadata = os.lstat(self.path)
        except OSError:
            raise ConfigurationError("provider binary identity changed during execution") from None
        finally:
            os.close(descriptor)
        expected = (
            self.device,
            self.inode,
            self.owner,
            self.size,
            self.mode,
            self.mtime_ns,
            self.ctime_ns,
        )
        actual = (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_uid),
            int(metadata.st_size),
            stat.S_IMODE(metadata.st_mode),
            getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1e9)),
            getattr(metadata, "st_ctime_ns", int(metadata.st_ctime * 1e9)),
        )
        path_actual = (
            int(path_metadata.st_dev),
            int(path_metadata.st_ino),
            int(path_metadata.st_uid),
            int(path_metadata.st_size),
            stat.S_IMODE(path_metadata.st_mode),
            getattr(path_metadata, "st_mtime_ns", int(path_metadata.st_mtime * 1e9)),
            getattr(path_metadata, "st_ctime_ns", int(path_metadata.st_ctime * 1e9)),
        )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or expected != actual
            or expected != path_actual
            or parent_chain != self.parent_chain
        ):
            raise ConfigurationError("provider binary identity changed during execution")
        if self.interpreter is not None:
            self.interpreter._verify_metadata(depth + 1)


def _require_executable_identity_argv(
    argv0: str, executable_identity: Optional[ExecutableIdentity]
) -> None:
    """Bind an issued executable identity to the exact spawned argv path."""

    if type(executable_identity) is not ExecutableIdentity:
        raise ConfigurationError("executable_identity must be ExecutableIdentity")
    if (
        type(argv0) is not str
        or not os.path.isabs(argv0)
        or os.path.normpath(argv0) != argv0
        or os.path.realpath(argv0) != argv0
    ):
        raise ConfigurationError(
            "subprocess argv executable must be canonical and absolute"
        )
    if argv0 != executable_identity.path:
        raise ConfigurationError(
            "subprocess argv executable does not match executable identity"
        )


class _SpawnVerifiedPath:
    """Revalidate one launch path when ``Popen`` consumes its argv entry."""

    __slots__ = ("_identity", "_path")

    def __init__(self, identity: ExecutableIdentity) -> None:
        if type(identity) is not ExecutableIdentity:
            raise ConfigurationError("launch identity is invalid")
        self._identity = identity
        self._path = identity.path

    def __fspath__(self) -> str:
        # subprocess calls os.fsencode on path-like argv entries in its final
        # argument preparation path.  This runtime-owned hook catches a target
        # swapped after the ordinary pre-Popen verification checkpoint.
        self._identity.verify_metadata()
        return self._path

    def __repr__(self) -> str:
        return "<_SpawnVerifiedPath>"


def _validated_launch_identities(
    argv: Sequence[str],
    executable_identity: ExecutableIdentity,
    launch_identities: Optional[Tuple[ExecutableIdentity, ...]],
) -> Tuple[ExecutableIdentity, ...]:
    """Bind every runtime-owned launch-prefix entry to an immutable identity."""

    if launch_identities is None:
        identities = (executable_identity,)
    else:
        if type(launch_identities) is not tuple:
            raise ConfigurationError("launch identities must be a tuple")
        identities = launch_identities
    if not 1 <= len(identities) <= 4 or len(argv) < len(identities):
        raise ConfigurationError("launch identities do not match subprocess argv")
    if identities[0] != executable_identity:
        raise ConfigurationError("launch executable identity changed")
    for index, identity in enumerate(identities):
        if type(identity) is not ExecutableIdentity or argv[index] != identity.path:
            raise ConfigurationError("launch identities do not match subprocess argv")
    return identities


def _guarded_spawn_argv(
    argv: Sequence[str], identities: Tuple[ExecutableIdentity, ...]
) -> list:
    """Return Popen argv whose verified prefix is checked at consumption time."""

    for identity in identities:
        identity.verify()
    return [
        *(_SpawnVerifiedPath(identity) for identity in identities),
        *argv[len(identities):],
    ]


def _verify_launch_identities(
    identities: Tuple[ExecutableIdentity, ...]
) -> None:
    for identity in identities:
        identity.verify_metadata()


@dataclass(frozen=True)
class _DirectoryRecord:
    path: str
    device: int
    inode: int
    owner: int
    mode: int


def _directory_records(path: str, *, private_final: bool) -> Tuple[_DirectoryRecord, ...]:
    """Capture a canonical directory and its security-relevant parent chain."""

    records = []
    current = os.path.sep
    components = [item for item in path.split(os.path.sep) if item]
    candidates = [current]
    for component in components:
        current = os.path.join(current, component)
        candidates.append(current)
    effective_uid = os.geteuid() if hasattr(os, "geteuid") else None
    for index, candidate in enumerate(candidates):
        try:
            metadata = os.lstat(candidate)
        except OSError:
            raise ConfigurationError("provider directory chain could not be inspected") from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ConfigurationError("provider directory chain must contain only directories")
        mode = stat.S_IMODE(metadata.st_mode)
        final = index == len(candidates) - 1
        if effective_uid is not None:
            if final and metadata.st_uid != effective_uid:
                raise ConfigurationError("provider directory must be owned by the current user")
            if not final and metadata.st_uid not in (0, effective_uid):
                raise ConfigurationError("provider directory parent has an unsafe owner")
        writable_by_others = mode & (stat.S_IWGRP | stat.S_IWOTH)
        if writable_by_others:
            sticky_parent = not final and bool(metadata.st_mode & stat.S_ISVTX)
            if not sticky_parent:
                raise ConfigurationError("provider directory chain has unsafe permissions")
        if final and private_final and mode != 0o700:
            raise ConfigurationError("persistent provider home must have owner-only rwx permissions")
        records.append(
            _DirectoryRecord(
                candidate,
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_uid,
                mode,
            )
        )
    return tuple(records)


class DirectoryPin:
    """Open-fd identity pin for a canonical directory pathname.

    CPython's portable ``subprocess`` API does not accept a directory fd for
    ``cwd`` on macOS or Linux, and using ``preexec_fn`` is unsafe in this
    threaded runtime.  The pin therefore rechecks the complete parent chain
    when ``Popen`` converts this PathLike, and again immediately after spawn
    while the fd remains open.  A same-UID process with rename authority still
    has a narrow race between final pathname conversion and the child's
    ``chdir``; device, inode, owner, and mode changes on either side fail closed.
    """

    __slots__ = (
        "_path",
        "_descriptor",
        "_records",
        "_private_final",
        "_close_lock",
        "_closed",
    )

    def __init__(self, path: str, *, private_final: bool = False) -> None:
        self._closed = True
        self._close_lock = threading.RLock()
        if type(path) is not str or not path or "\x00" in path or not os.path.isabs(path):
            raise ConfigurationError("provider directory path must be absolute")
        normalized = os.path.normpath(path)
        if len(os.fsencode(normalized)) > 16 * 1024:
            raise ConfigurationError("provider directory path is invalid")
        if os.path.realpath(normalized) != normalized:
            raise ConfigurationError("provider directory must not use symlinks")
        records = _directory_records(normalized, private_final=private_final)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(normalized, flags)
        except OSError:
            raise ConfigurationError("provider directory could not be opened safely") from None
        try:
            metadata = os.fstat(descriptor)
            final = records[-1]
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_dev != final.device
                or metadata.st_ino != final.inode
                or metadata.st_uid != final.owner
                or stat.S_IMODE(metadata.st_mode) != final.mode
            ):
                raise ConfigurationError("provider directory changed while it was pinned")
        except BaseException:
            os.close(descriptor)
            raise
        self._path = normalized
        self._descriptor = descriptor
        self._records = records
        self._private_final = private_final
        self._closed = False

    @property
    def path(self) -> str:
        return self._path

    @property
    def closed(self) -> bool:
        with self._close_lock:
            return self._closed

    def __fspath__(self) -> str:
        # ``Popen`` calls os.fsencode(cwd), so this is the last portable hook
        # before its internal fork/exec path consumes the pathname.
        self.verify()
        return self._path

    def verify(self) -> None:
        with self._close_lock:
            if self._closed:
                raise ConfigurationError("provider directory pin is closed")
            try:
                metadata = os.fstat(self._descriptor)
            except OSError:
                raise ConfigurationError("provider directory pin could not be verified") from None
            final = self._records[-1]
            current = _directory_records(
                self._path,
                private_final=self._private_final,
            )
            if (
                current != self._records
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_dev != final.device
                or metadata.st_ino != final.inode
                or metadata.st_uid != final.owner
                or stat.S_IMODE(metadata.st_mode) != final.mode
            ):
                raise ConfigurationError("provider directory changed before execution")

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            descriptor = self._descriptor
            # Terminalize ownership before entering ``close(2)``.  POSIX close
            # failures are ambiguous: the open-file description may already
            # be gone and this number may be reused immediately, even for the
            # same inode.  Consequently this owner makes exactly one close
            # attempt.  A rare ambiguous one-fd leak is safer than closing a
            # replacement open-file description on any retry.
            self._descriptor = -1
            self._closed = True
            os.close(descriptor)

    def __enter__(self) -> "DirectoryPin":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self.close()
        except BaseException:
            if exc_type is None:
                raise

    def __repr__(self) -> str:
        return "<DirectoryPin active={}>".format(not self._closed)


def private_persistent_home(path: str, *, create: bool = True) -> str:
    """Create/validate an explicit same-user, owner-only HOME directory.

    No component may resolve through a symlink.  Only the final directory is
    created; callers must deliberately provision its parent.
    """

    if type(path) is not str or not path or "\x00" in path or not os.path.isabs(path):
        raise ConfigurationError("persistent provider home path is invalid")
    normalized = os.path.normpath(path)
    if len(os.fsencode(normalized)) > 16 * 1024:
        raise ConfigurationError("persistent provider home path is invalid")
    if os.path.realpath(normalized) != normalized:
        raise ConfigurationError("persistent provider home must not use symlinks")
    created = False
    try:
        metadata = os.lstat(normalized)
    except FileNotFoundError:
        if not create:
            raise ConfigurationError("persistent provider home does not exist") from None
        try:
            os.mkdir(normalized, 0o700)
            created = True
        except OSError:
            raise ConfigurationError("persistent provider home could not be created") from None
        except BaseException:
            # Fault injection can interrupt immediately after mkdir returned
            # through a wrapper.  Remove only the still-empty final component;
            # never recurse through a caller-owned persistent path.
            try:
                os.rmdir(normalized)
            except BaseException:
                pass
            raise
    except OSError:
        raise ConfigurationError("persistent provider home could not be inspected") from None
    else:
        if stat.S_ISLNK(metadata.st_mode):
            raise ConfigurationError("persistent provider home must not be a symlink")
    try:
        pin = DirectoryPin(normalized, private_final=True)
        pin.__exit__(None, None, None)
    except BaseException:
        if created:
            try:
                os.rmdir(normalized)
            except BaseException:
                pass
        raise
    return normalized


def validated_workspace(path: str) -> str:
    """Return an existing, same-user, non-symlink project directory.

    Provider chat execution is deliberately tied to a caller-selected project
    boundary.  Resolving a symlink and then using its target is not equivalent:
    the displayed project and the directory given to the provider could differ.
    Consequently every path component must already be canonical and the final
    component is opened with ``O_NOFOLLOW`` immediately before it is accepted.
    """

    if type(path) is not str or not path or "\x00" in path or not os.path.isabs(path):
        raise ConfigurationError("provider workspace path must be absolute")
    try:
        pin = DirectoryPin(path)
    except ConfigurationError as exc:
        message = str(exc)
        if "absolute" in message:
            raise ConfigurationError("provider workspace path must be absolute") from None
        if "symlink" in message:
            raise ConfigurationError("provider workspace must not use symlinks") from None
        raise ConfigurationError(
            "provider workspace must be an existing directory with a secure parent chain"
        ) from None
    result = pin.path
    pin.__exit__(None, None, None)
    return result


def validate_positive_timeout(value: Any) -> float:
    if type(value) not in (int, float) or value <= 0:
        raise ConfigurationError("timeout must be a finite positive number")
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        raise ConfigurationError("timeout must be a finite positive number") from None
    if not math.isfinite(numeric):
        raise ConfigurationError("timeout must be a finite positive number")
    return numeric


def strict_json_loads(payload: Any) -> Any:
    """Decode RFC JSON while rejecting duplicate keys and non-finite values."""

    def object_pairs(pairs: Any) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    return json.loads(
        payload,
        object_pairs_hook=object_pairs,
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError("non-finite JSON number")
        ),
    )


class _OwnedTemporaryDirectory:
    """Identity-bound temporary root whose owner exists before ``mkdir``.

    ``tempfile.TemporaryDirectory(...)`` can create its directory and then
    raise before the caller receives the owner.  This owner chooses a canonical
    cryptographically random path in its constructor, is stored by the runtime,
    and only then creates the directory.  Cleanup is restricted to the exact
    device/inode captured for that 0700 same-user directory.  If creation or
    removal raises ambiguously, ownership is retained for a later bounded retry
    instead of recursively deleting an unverified pathname.
    """

    __slots__ = (
        "_cleaned",
        "_created",
        "_identity",
        "_lock",
        "_ownership_possible",
        "name",
    )

    def __init__(self, *, prefix: str) -> None:
        if (
            type(prefix) is not str
            or not prefix
            or len(prefix) > 128
            or re.fullmatch(r"[A-Za-z0-9_.-]+", prefix) is None
        ):
            raise ConfigurationError("temporary directory prefix is invalid")
        base = os.path.realpath(tempfile.gettempdir())
        if (
            not os.path.isabs(base)
            or os.path.normpath(base) != base
            or os.path.realpath(base) != base
        ):
            raise ConfigurationError("temporary directory root is invalid")
        name = os.path.join(base, prefix + secrets.token_hex(16))
        if len(os.fsencode(name)) > 16 * 1024 or os.path.realpath(name) != name:
            raise ConfigurationError("temporary directory path is invalid")
        self.name = name
        self._lock = threading.RLock()
        self._identity: Optional[Tuple[int, int, int, int]] = None
        self._created = False
        self._ownership_possible = False
        self._cleaned = False

    @property
    def cleaned(self) -> bool:
        with self._lock:
            return self._cleaned

    @property
    def has_resources(self) -> bool:
        with self._lock:
            return not self._cleaned and (
                self._created or self._ownership_possible
            )

    @staticmethod
    def _identity_for(metadata: os.stat_result) -> Tuple[int, int, int, int]:
        return (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_uid),
            stat.S_IMODE(metadata.st_mode),
        )

    def _bind_current(self) -> bool:
        try:
            metadata = os.lstat(self.name)
        except FileNotFoundError:
            return False
        except OSError:
            return False
        effective_uid = os.geteuid() if hasattr(os, "geteuid") else metadata.st_uid
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != effective_uid
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            return False
        self._identity = self._identity_for(metadata)
        self._created = True
        return True

    def create(self) -> "_OwnedTemporaryDirectory":
        with self._lock:
            if self._created or self._ownership_possible or self._cleaned:
                raise ConfigurationError("temporary directory owner is already used")
            if os.path.lexists(self.name):
                raise ConfigurationError("temporary directory path collision")
            self._ownership_possible = True
            try:
                os.mkdir(self.name, 0o700)
            except BaseException:
                # A wrapper can create the directory and raise before returning.
                # Bind it when it is safely identifiable; otherwise retain the
                # possible owner so cleanup can retry inspection later.
                if not os.path.lexists(self.name):
                    self._ownership_possible = False
                else:
                    self._bind_current()
                raise
            self._created = True
            if not self._bind_current():
                raise ConfigurationError(
                    "temporary directory could not be identity-bound"
                )
            return self

    def cleanup(self) -> None:
        with self._lock:
            if self._cleaned:
                return
            if not self._created and not self._ownership_possible:
                self._cleaned = True
                return
            try:
                metadata = os.lstat(self.name)
            except FileNotFoundError:
                self._created = False
                self._ownership_possible = False
                self._identity = None
                self._cleaned = True
                return
            if self._identity is None and not self._bind_current():
                raise ConfigurationError(
                    "owned temporary directory identity could not be verified"
                )
            assert self._identity is not None
            current_identity = self._identity_for(metadata)
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or current_identity != self._identity
            ):
                # The owned inode is no longer reachable at this pathname.  Do
                # not touch a replacement directory, even if it has the same
                # owner/mode or was recreated under the same name.
                self._created = False
                self._ownership_possible = False
                self._identity = None
                self._cleaned = True
                return
            try:
                shutil.rmtree(self.name)
            except FileNotFoundError:
                pass
            except BaseException:
                try:
                    after = os.lstat(self.name)
                except FileNotFoundError:
                    after = None
                except OSError:
                    raise
                if after is not None and self._identity_for(after) == self._identity:
                    raise
                # Removal completed before the exception, or a replacement won
                # the pathname.  In either case our inode is no longer owned.
            self._created = False
            self._ownership_possible = False
            self._identity = None
            self._cleaned = True


class _SpawnEnvironment(dict):
    """Dict accepted by subprocess that revalidates HOME during serialization."""

    __slots__ = ("_owner",)

    def __init__(self, owner: "IsolatedEnvironment", values: Mapping[str, str]) -> None:
        super().__init__(values)
        self._owner = owner

    def items(self):
        self._owner.verify_for_spawn()
        return super().items()


class IsolatedEnvironment:
    """Context-managed HOME/TMPDIR with a minimal inherited environment."""

    def __init__(
        self,
        provider_env: Optional[Mapping[str, str]] = None,
        *,
        allowed_provider_keys: Iterable[str] = (),
        persistent_home: Optional[str] = None,
    ) -> None:
        self._provider_env: Dict[str, str] = {}
        try:
            source = provider_env if provider_env is not None else {}
            iterator = iter(source.items())
            for index, pair in enumerate(iterator):
                if index >= 256:
                    raise ConfigurationError("provider environment exceeds 256 entries")
                try:
                    key, value = pair
                except (TypeError, ValueError):
                    raise ConfigurationError("provider environment mapping is malformed") from None
                if (
                    type(key) is not str
                    or type(value) is not str
                    or not _ENV_KEY_RE.fullmatch(key)
                    or key in _BASE_ENV_KEYS
                    or key in {"HOME", "TMPDIR"}
                ):
                    raise ConfigurationError("invalid provider environment entry")
                try:
                    encoded_value = value.encode("utf-8", "strict")
                except UnicodeError:
                    raise ConfigurationError("invalid provider environment value") from None
                if b"\x00" in encoded_value or len(encoded_value) > 64 * 1024:
                    raise ConfigurationError("invalid provider environment value")
                if key in self._provider_env:
                    raise ConfigurationError("duplicate provider environment key")
                self._provider_env[key] = value
        except ConfigurationError:
            raise
        except Exception:
            raise ConfigurationError("provider environment mapping is malformed") from None
        allowed = set()
        try:
            for index, key in enumerate(allowed_provider_keys):
                if index >= 256:
                    raise ConfigurationError("provider environment allowlist exceeds 256 entries")
                if (
                    type(key) is not str
                    or not _ENV_KEY_RE.fullmatch(key)
                    or key in _BASE_ENV_KEYS
                    or key in {"HOME", "TMPDIR"}
                ):
                    raise ConfigurationError("invalid provider environment allowlist key")
                allowed.add(key)
        except ConfigurationError:
            raise
        except Exception:
            raise ConfigurationError("provider environment allowlist is malformed") from None
        self._allowed = frozenset(allowed)
        self._persistent_home = persistent_home
        self._temporary: Optional[_OwnedTemporaryDirectory] = None
        self._home_pin: Optional[DirectoryPin] = None
        self._tmp_pin: Optional[DirectoryPin] = None
        self.env: Dict[str, str] = {}

    @property
    def secret_values(self) -> tuple[str, ...]:
        return tuple(self._provider_env.values())

    @property
    def has_resources(self) -> bool:
        """Whether cleanup ownership remains, independent of successful entry."""

        return bool(
            self.env
            or self._temporary is not None
            or self._home_pin is not None
            or self._tmp_pin is not None
        )

    def __enter__(self) -> "IsolatedEnvironment":
        if self.has_resources:
            raise ConfigurationError("isolated environment is already active")
        unknown = set(self._provider_env) - self._allowed
        if unknown:
            raise ConfigurationError("provider environment key is not allowlisted")
        try:
            # Store ownership before the fallible mkdir so a create-then-raise
            # fault can never orphan the runtime root.
            self._temporary = _OwnedTemporaryDirectory(prefix="unified-cli-ext-")
            self._temporary.create()
            root = self._temporary.name
            home = (
                private_persistent_home(self._persistent_home)
                if self._persistent_home is not None
                else os.path.join(root, "home")
            )
            tmp = os.path.join(root, "tmp")
            if self._persistent_home is None:
                os.mkdir(home, 0o700)
            os.mkdir(tmp, 0o700)
            self._home_pin = DirectoryPin(home, private_final=True)
            self._tmp_pin = DirectoryPin(tmp, private_final=True)
            env = {key: os.environ[key] for key in _BASE_ENV_KEYS if key in os.environ}
            env.update({"HOME": home, "TMPDIR": tmp})
            env.update(self._provider_env)
            self.env = _SpawnEnvironment(self, env)
        except BaseException as failure:
            cleanup_failure = None
            for _ in range(2):
                if not self.has_resources:
                    break
                try:
                    self._cleanup()
                except BaseException as caught:
                    cleanup_failure = caught
            if self.has_resources and cleanup_failure is not None:
                raise cleanup_failure from failure
            if isinstance(failure, OSError):
                raise ConfigurationError("could not create isolated environment") from None
            raise
        return self

    def verify_for_spawn(self) -> None:
        if self._home_pin is None or self._tmp_pin is None:
            raise ConfigurationError("isolated environment is not active")
        self._home_pin.verify()
        self._tmp_pin.verify()

    def verify_after_spawn(self) -> None:
        self.verify_for_spawn()

    def _cleanup(self) -> None:
        failure = None
        try:
            self.env.clear()
        except BaseException as caught:
            failure = caught
        else:
            self.env = {}
        for name in ("_home_pin", "_tmp_pin"):
            pin = getattr(self, name)
            if pin is not None:
                try:
                    pin.close()
                except BaseException as caught:
                    if failure is None:
                        failure = caught
                if pin.closed:
                    setattr(self, name, None)
        temporary = self._temporary
        if temporary is not None:
            try:
                temporary.cleanup()
            except BaseException as caught:
                if failure is None:
                    failure = caught
            if temporary.cleaned:
                self._temporary = None
        if failure is not None:
            raise failure

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self._cleanup()
        except BaseException as first_failure:
            if self.has_resources:
                try:
                    self._cleanup()
                except BaseException as cleanup_failure:
                    if exc_type is None:
                        raise
                    raise cleanup_failure from exc
            if exc_type is None:
                raise first_failure


def redact_diagnostics(text: str, secrets: Iterable[str] = (), max_chars: int = 4096) -> str:
    """Return a bounded diagnostic without truncation-created secret fragments.

    Only a bounded prefix plus at most one maximum-length secret of overlap is
    inspected.  Secret spans are located against that overlap before the
    visible prefix is emitted, so a secret crossing ``max_chars`` is redacted
    instead of being shortened into a disclosure.
    """

    if type(text) is not str:
        raise TypeError("diagnostic text must be a string")
    if type(max_chars) is not int or max_chars <= 0 or max_chars > 64 * 1024:
        raise ValueError("max_chars must be between one and 65536")
    bounded_secrets = []
    aggregate_secret_bytes = 0
    for index, item in enumerate(secrets):
        if index >= 256:
            return "[REDACTED]"[:max_chars]
        if type(item) is str and item:
            if len(item) > _MAX_DIAGNOSTIC_SECRET_CHARS:
                return "[REDACTED]"[:max_chars]
            aggregate_secret_bytes += len(
                item.encode("utf-8", "surrogatepass")
            )
            if aggregate_secret_bytes > _MAX_DIAGNOSTIC_SECRET_BYTES:
                # Matching attacker-sized pattern sets is unnecessary for a
                # diagnostic.  Fail safely to a fully redacted message instead.
                return "[REDACTED]"[:max_chars]
            bounded_secrets.append(item)
    unique_secrets = tuple(
        sorted(set(bounded_secrets), key=lambda value: (-len(value), value))
    )
    visible_length = min(len(text), max_chars)
    maximum_secret = max((len(item) for item in unique_secrets), default=0)
    scan_length = min(len(text), visible_length + max(0, maximum_secret - 1))
    scanned = text[:scan_length]
    intervals = []
    match_count = 0
    for secret in unique_secrets:
        start = scanned.find(secret)
        while start >= 0 and start < visible_length:
            end = min(visible_length, start + len(secret))
            intervals.append((start, end))
            match_count += 1
            if match_count > _MAX_DIAGNOSTIC_MATCHES:
                return "[REDACTED]"[:max_chars]
            start = scanned.find(secret, start + 1)
    pieces = []
    offset = 0
    for start, end in sorted(intervals):
        if end <= offset:
            continue
        if start > offset:
            pieces.append(scanned[offset:start])
        pieces.append("[REDACTED]")
        offset = max(offset, end)
    if offset < visible_length:
        pieces.append(scanned[offset:visible_length])
    redacted = "".join(pieces)
    redacted = _SECRET_PATTERN.sub(lambda match: match.group(1) + "[REDACTED]", redacted)
    redacted = redacted.replace("\x00", "")
    return redacted[:max_chars]


__all__ = [
    "CancellationToken",
    "DirectoryPin",
    "ExecutableIdentity",
    "IsolatedEnvironment",
    "TransportLimits",
    "redact_diagnostics",
    "private_persistent_home",
    "validated_workspace",
    "strict_json_loads",
    "validate_positive_timeout",
]
