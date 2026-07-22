"""Immutable local installation/acquisition receipts for provider launchers.

The receipt is evidence about files at an explicitly named local installation
boundary.  It is not a signature, an attestation, or a claim that a vendor
published those files.  Capture never searches ``PATH``, invokes a shell or a
subprocess, reads credentials, installs a package, or performs network I/O.

Executable and script content is hashed once during capture.  Later
verification normally checks the captured file and parent-directory metadata.
The npm manifest is the deliberate exception: its parsed content is part of
the binding, so verification re-reads, re-hashes, and strictly parses it.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple

from ..errors import ConfigurationError
from ..transports.security import ExecutableIdentity


INSTALLATION_RECEIPT_ABI_V1 = 1

_MAX_PATH_BYTES = 16 * 1024
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
_MAX_SHEBANG_BYTES = 4096
_MAX_SYMLINKS = 16
_MAX_JSON_DEPTH = 32
_MAX_JSON_ITEMS = 8192
_MAX_JSON_STRING_CHARS = 64 * 1024
_MAX_RECEIPT_UTF8_BYTES = 1024 * 1024
_MAX_IDENTITY_INTEGER = (1 << 127) - 1

_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")
_NPM_NAME_RE = re.compile(
    r"^(?:@[a-z0-9][a-z0-9._-]{0,126}/)?[a-z0-9][a-z0-9._-]{0,126}$"
)
_SEMVER_RE = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class InstallationReceiptKindV1(str, Enum):
    """The local launch shape described by a v1 receipt."""

    DIRECT_EXECUTABLE = "direct_executable"
    NPM_PACKAGE_LAUNCHER = "npm_package_launcher"


class DistributionTypeV1(str, Enum):
    """The expected acquisition distribution type."""

    VENDOR_EXECUTABLE = "vendor_executable"
    NPM_PACKAGE = "npm_package"


def _fail(message: str) -> ConfigurationError:
    return ConfigurationError(message)


def _safe_text(
    value: object,
    *,
    label: str,
    maximum: int,
    empty: bool = False,
) -> str:
    if type(value) is not str or (not value and not empty) or len(value) > maximum:
        raise _fail("{} is invalid".format(label))
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        raise _fail("{} is invalid".format(label)) from None
    for character in value:
        category = unicodedata.category(character)
        if character == "\x00" or category.startswith("C") or category in {"Zl", "Zp"}:
            raise _fail("{} is invalid".format(label))
    return value


def _provider_id(value: object) -> str:
    result = _safe_text(value, label="receipt provider id", maximum=64)
    if _PROVIDER_ID_RE.fullmatch(result) is None:
        raise _fail("receipt provider id is invalid")
    return result


def _version(value: object) -> str:
    result = _safe_text(value, label="distribution version", maximum=128)
    if _SEMVER_RE.fullmatch(result) is None:
        raise _fail("distribution version is not normalized")
    return result


def _basename(value: object) -> str:
    result = _safe_text(value, label="launcher basename", maximum=255)
    if result in {".", ".."} or os.path.basename(result) != result:
        raise _fail("launcher basename is invalid")
    return result


def _absolute_path(value: object, *, label: str, canonical: bool) -> str:
    if type(value) is not str or not value or "\x00" in value or not os.path.isabs(value):
        raise _fail("{} must be an absolute path".format(label))
    try:
        encoded = os.fsencode(value)
    except (TypeError, UnicodeError):
        raise _fail("{} is invalid".format(label)) from None
    if len(encoded) > _MAX_PATH_BYTES:
        raise _fail("{} is invalid".format(label))
    normalized = os.path.normpath(value)
    if normalized != value:
        raise _fail("{} must be normalized".format(label))
    if canonical:
        try:
            real_path = os.path.realpath(normalized)
        except (OSError, ValueError):
            raise _fail("{} could not be canonicalized".format(label)) from None
        if real_path != normalized:
            raise _fail("{} must be canonical".format(label))
    return normalized


def _within(path: str, boundary: str) -> bool:
    try:
        return os.path.commonpath((path, boundary)) == boundary
    except (ValueError, OSError):
        return False


def _identity_integer(value: object, *, label: str, maximum: int) -> int:
    if type(value) is not int or value < 0 or value > maximum:
        raise _fail("{} is invalid".format(label))
    return value


def _expected_directory_paths(path: str) -> Tuple[str, ...]:
    current = os.path.sep
    result = [current]
    for component in (item for item in path.split(os.path.sep) if item):
        current = os.path.join(current, component)
        result.append(current)
    return tuple(result)


@dataclass(frozen=True)
class DirectoryIdentityV1:
    """Security-relevant identity of one directory path component."""

    path: str
    device: int
    inode: int
    owner: int
    mode: int

    def __post_init__(self) -> None:
        _absolute_path(self.path, label="directory identity path", canonical=False)
        for value in (self.device, self.inode, self.owner):
            _identity_integer(
                value,
                label="directory identity",
                maximum=_MAX_IDENTITY_INTEGER,
            )
        _identity_integer(self.mode, label="directory mode", maximum=0o7777)
        effective_uid = os.geteuid() if hasattr(os, "geteuid") else None
        if effective_uid is None or self.owner not in (0, effective_uid):
            raise _fail("directory identity owner is unsafe")
        if self.mode & (stat.S_IWGRP | stat.S_IWOTH) and not (
            self.mode & stat.S_ISVTX
        ):
            raise _fail("directory identity permissions are unsafe")


def _validate_directory_chain_shape(
    chain: object, *, endpoint: str, label: str
) -> Tuple[DirectoryIdentityV1, ...]:
    if type(chain) is not tuple or not chain or len(chain) > 1024:
        raise _fail("{} is invalid".format(label))
    if any(type(item) is not DirectoryIdentityV1 for item in chain):
        raise _fail("{} is invalid".format(label))
    if tuple(item.path for item in chain) != _expected_directory_paths(endpoint):
        raise _fail("{} is invalid".format(label))
    return chain


def _directory_chain(path: str) -> Tuple[DirectoryIdentityV1, ...]:
    if not hasattr(os, "geteuid"):
        raise _fail("installation ownership could not be verified")
    effective_uid = os.geteuid()
    current = os.path.sep
    candidates = [current]
    for component in (item for item in path.split(os.path.sep) if item):
        current = os.path.join(current, component)
        candidates.append(current)
    result = []
    for candidate in candidates:
        try:
            metadata = os.lstat(candidate)
        except OSError:
            raise _fail("installation directory chain could not be inspected") from None
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise _fail("installation directory chain is invalid")
        if metadata.st_uid not in (0, effective_uid):
            raise _fail("installation directory owner is unsafe")
        if mode & (stat.S_IWGRP | stat.S_IWOTH) and not (
            metadata.st_mode & stat.S_ISVTX
        ):
            raise _fail("installation directory permissions are unsafe")
        result.append(
            DirectoryIdentityV1(
                candidate,
                int(metadata.st_dev),
                int(metadata.st_ino),
                int(metadata.st_uid),
                mode,
            )
        )
    return tuple(result)


def _verify_directory_chain(
    path: str, expected: Tuple[DirectoryIdentityV1, ...]
) -> None:
    if type(expected) is not tuple or len(expected) > 1024:
        raise _fail("installation directory binding is invalid")
    if _directory_chain(path) != expected:
        raise _fail("installation directory binding changed")


@dataclass(frozen=True)
class ArtifactIdentityV1:
    """Bound identity and authoritative capture digest for one regular file."""

    path: str
    sha256: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    mode: int
    owner: int
    parent_chain: Tuple[DirectoryIdentityV1, ...]

    def __post_init__(self) -> None:
        _absolute_path(self.path, label="artifact identity path", canonical=True)
        if (
            type(self.sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None
        ):
            raise _fail("artifact digest is invalid")
        for value in (self.device, self.inode, self.mtime_ns, self.ctime_ns, self.owner):
            _identity_integer(
                value,
                label="artifact identity",
                maximum=_MAX_IDENTITY_INTEGER,
            )
        _identity_integer(
            self.size,
            label="artifact size",
            maximum=_MAX_EXECUTABLE_BYTES,
        )
        _identity_integer(self.mode, label="artifact mode", maximum=0o7777)
        effective_uid = os.geteuid() if hasattr(os, "geteuid") else None
        if effective_uid is None or self.owner not in (0, effective_uid):
            raise _fail("artifact identity owner is unsafe")
        if self.mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise _fail("artifact identity permissions are unsafe")
        _validate_directory_chain_shape(
            self.parent_chain,
            endpoint=os.path.dirname(self.path),
            label="artifact parent binding",
        )

    def verify_metadata(self, *, executable: bool) -> None:
        if type(executable) is not bool:
            raise _fail("artifact verification mode is invalid")
        try:
            real_path = os.path.realpath(self.path)
        except (OSError, ValueError):
            raise _fail("installation artifact changed") from None
        if real_path != self.path:
            raise _fail("installation artifact changed")
        parent_chain = _directory_chain(os.path.dirname(self.path))
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags)
        except OSError:
            raise _fail("installation artifact changed") from None
        try:
            metadata = os.fstat(descriptor)
            path_metadata = os.lstat(self.path)
        except OSError:
            raise _fail("installation artifact changed") from None
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
        actual = _metadata_record(metadata)
        path_actual = _metadata_record(path_metadata)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or expected != actual
            or expected != path_actual
            or parent_chain != self.parent_chain
            or (executable and not metadata.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
        ):
            raise _fail("installation artifact changed")


def _metadata_record(metadata: os.stat_result) -> Tuple[int, ...]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_uid),
        int(metadata.st_size),
        stat.S_IMODE(metadata.st_mode),
        int(getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1e9))),
        int(getattr(metadata, "st_ctime_ns", int(metadata.st_ctime * 1e9))),
    )


def _capture_artifact(
    path: str,
    *,
    maximum_bytes: int,
    executable: bool,
    retain_content: bool,
) -> Tuple[ArtifactIdentityV1, bytes]:
    canonical = _absolute_path(path, label="installation artifact path", canonical=True)
    parent_chain = _directory_chain(os.path.dirname(canonical))
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(canonical, flags)
    except OSError:
        raise _fail("installation artifact could not be opened safely") from None
    digest = hashlib.sha256()
    retained = bytearray()
    header = bytearray()
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise _fail("installation artifact must be a regular file")
        effective_uid = os.geteuid() if hasattr(os, "geteuid") else None
        if effective_uid is None or metadata.st_uid not in (0, effective_uid):
            raise _fail("installation artifact owner is unsafe")
        if metadata.st_size < 0 or metadata.st_size > maximum_bytes:
            raise _fail("installation artifact size is outside the allowed range")
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise _fail("installation artifact permissions are unsafe")
        if executable and not metadata.st_mode & (
            stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        ):
            raise _fail("installation target is not executable")
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise _fail("installation artifact size is outside the allowed range")
            digest.update(chunk)
            if retain_content:
                retained.extend(chunk)
            elif len(header) < _MAX_SHEBANG_BYTES:
                header.extend(chunk[: _MAX_SHEBANG_BYTES - len(header)])
        metadata_after = os.fstat(descriptor)
        path_metadata = os.lstat(canonical)
        parent_after = _directory_chain(os.path.dirname(canonical))
        if (
            _metadata_record(metadata_after) != _metadata_record(metadata)
            or _metadata_record(path_metadata) != _metadata_record(metadata)
            or parent_after != parent_chain
            or total != metadata.st_size
        ):
            raise _fail("installation artifact changed while it was captured")
    except ConfigurationError:
        raise
    except (OSError, OverflowError, ValueError):
        raise _fail("installation artifact could not be captured") from None
    finally:
        os.close(descriptor)
    identity = ArtifactIdentityV1(
        path=canonical,
        sha256=digest.hexdigest(),
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        size=int(metadata.st_size),
        mtime_ns=int(getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1e9))),
        ctime_ns=int(getattr(metadata, "st_ctime_ns", int(metadata.st_ctime * 1e9))),
        mode=stat.S_IMODE(metadata.st_mode),
        owner=int(metadata.st_uid),
        parent_chain=parent_chain,
    )
    return identity, bytes(retained if retain_content else header)


def _artifact_from_executable(identity: ExecutableIdentity) -> ArtifactIdentityV1:
    return ArtifactIdentityV1(
        path=identity.path,
        sha256=identity.sha256,
        device=identity.device,
        inode=identity.inode,
        size=identity.size,
        mtime_ns=identity.mtime_ns,
        ctime_ns=identity.ctime_ns,
        mode=identity.mode,
        owner=identity.owner,
        parent_chain=tuple(
            DirectoryIdentityV1(path, device, inode, owner, mode)
            for path, device, inode, owner, mode in identity.parent_chain
        ),
    )


def _executable_from_artifact(identity: ArtifactIdentityV1) -> ExecutableIdentity:
    return ExecutableIdentity(
        path=identity.path,
        sha256=identity.sha256,
        device=identity.device,
        inode=identity.inode,
        size=identity.size,
        mtime_ns=identity.mtime_ns,
        mode=identity.mode,
        owner=identity.owner,
        parent_chain=tuple(
            (item.path, item.device, item.inode, item.owner, item.mode)
            for item in identity.parent_chain
        ),
        interpreter=None,
        ctime_ns=identity.ctime_ns,
    )


def _validate_executable_identity_shape(
    identity: object, *, depth: int = 0, ancestors: Tuple[int, ...] = ()
) -> ExecutableIdentity:
    if type(identity) is not ExecutableIdentity or depth > 4 or id(identity) in ancestors:
        raise _fail("installation executable identity is invalid")
    _absolute_path(identity.path, label="executable identity path", canonical=True)
    if type(identity.sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", identity.sha256) is None:
        raise _fail("installation executable identity is invalid")
    for value in (
        identity.device,
        identity.inode,
        identity.mtime_ns,
        identity.ctime_ns,
        identity.owner,
    ):
        _identity_integer(
            value,
            label="executable identity",
            maximum=_MAX_IDENTITY_INTEGER,
        )
    _identity_integer(
        identity.size,
        label="executable identity size",
        maximum=_MAX_EXECUTABLE_BYTES,
    )
    _identity_integer(identity.mode, label="executable identity mode", maximum=0o7777)
    effective_uid = os.geteuid() if hasattr(os, "geteuid") else None
    if (
        effective_uid is None
        or identity.owner not in (0, effective_uid)
        or identity.mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not identity.mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    ):
        raise _fail("installation executable identity is unsafe")
    if type(identity.parent_chain) is not tuple or not identity.parent_chain:
        raise _fail("installation executable parent binding is invalid")
    records = []
    for record in identity.parent_chain:
        if type(record) is not tuple or len(record) != 5:
            raise _fail("installation executable parent binding is invalid")
        path, device, inode, owner, mode = record
        records.append(DirectoryIdentityV1(path, device, inode, owner, mode))
    if tuple(item.path for item in records) != _expected_directory_paths(
        os.path.dirname(identity.path)
    ):
        raise _fail("installation executable parent binding is invalid")
    if identity.interpreter is not None:
        _validate_executable_identity_shape(
            identity.interpreter,
            depth=depth + 1,
            ancestors=ancestors + (id(identity),),
        )
    return identity


def _reject_constant(value: str) -> Any:
    del value
    raise ValueError("non-finite number")


def _bounded_int(value: str) -> int:
    if len(value) > 128:
        raise ValueError("integer is too large")
    return int(value)


def _bounded_float(value: str) -> float:
    if len(value) > 128:
        raise ValueError("number is too large")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("number is non-finite")
    return result


def _unique_object(pairs: Any) -> Dict[str, Any]:
    result = {}  # type: Dict[str, Any]
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _validate_json_shape(value: Any, depth: int = 0, budget: Optional[list] = None) -> None:
    if budget is None:
        budget = [_MAX_JSON_ITEMS]
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("metadata is too deep")
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError("metadata has too many items")
    if type(value) is str:
        if len(value) > _MAX_JSON_STRING_CHARS:
            raise ValueError("metadata string is too large")
        value.encode("utf-8", "strict")
    elif type(value) is list:
        for item in value:
            _validate_json_shape(item, depth + 1, budget)
    elif type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or len(key) > _MAX_JSON_STRING_CHARS:
                raise ValueError("metadata key is invalid")
            _validate_json_shape(item, depth + 1, budget)
    elif type(value) is float and not math.isfinite(value):
        raise ValueError("metadata number is non-finite")
    elif type(value) not in (type(None), bool, int, float):
        raise ValueError("metadata value is invalid")


def _parse_manifest(content: bytes) -> Dict[str, Any]:
    try:
        text = content.decode("utf-8", "strict")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_int=_bounded_int,
            parse_float=_bounded_float,
        )
        _validate_json_shape(value)
    except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError):
        raise _fail("package manifest is invalid") from None
    if type(value) is not dict:
        raise _fail("package manifest is invalid")
    return value


def _manifest_bin_path(
    manifest: Dict[str, Any], *, package_name: str, version: str, executable: str
) -> str:
    if type(manifest.get("name")) is not str or manifest.get("name") != package_name:
        raise _fail("package manifest identity does not match receipt")
    if type(manifest.get("version")) is not str or manifest.get("version") != version:
        raise _fail("package manifest identity does not match receipt")
    bin_value = manifest.get("bin")
    if type(bin_value) is str:
        if executable != package_name.rsplit("/", 1)[-1]:
            raise _fail("package manifest launcher does not match receipt")
        result = bin_value
    elif type(bin_value) is dict:
        if len(bin_value) > 128 or executable not in bin_value:
            raise _fail("package manifest launcher does not match receipt")
        for key, value in bin_value.items():
            _basename(key)
            if type(value) is not str:
                raise _fail("package manifest launcher does not match receipt")
        result = bin_value[executable]
    else:
        raise _fail("package manifest launcher does not match receipt")
    result = _safe_text(result, label="package manifest launcher", maximum=4096)
    if os.path.isabs(result):
        raise _fail("package manifest launcher is outside the package")
    normalized = os.path.normpath(result)
    if normalized in {".", ".."} or normalized.startswith(".." + os.path.sep):
        raise _fail("package manifest launcher is outside the package")
    return normalized


@dataclass(frozen=True)
class SymlinkIdentityV1:
    """One bounded npm launcher symlink hop and its normalized destination."""

    path: str
    target_text: str
    resolved_target: str
    device: int
    inode: int
    owner: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    parent_chain: Tuple[DirectoryIdentityV1, ...]

    def __post_init__(self) -> None:
        _absolute_path(self.path, label="launcher symlink path", canonical=False)
        _safe_text(self.target_text, label="launcher symlink target", maximum=_MAX_PATH_BYTES)
        _absolute_path(
            self.resolved_target,
            label="launcher symlink destination",
            canonical=False,
        )
        for value in (
            self.device,
            self.inode,
            self.owner,
            self.mtime_ns,
            self.ctime_ns,
        ):
            _identity_integer(
                value,
                label="launcher symlink identity",
                maximum=_MAX_IDENTITY_INTEGER,
            )
        _identity_integer(
            self.mode,
            label="launcher symlink mode",
            maximum=0o7777,
        )
        _identity_integer(
            self.size,
            label="launcher symlink size",
            maximum=_MAX_PATH_BYTES,
        )
        effective_uid = os.geteuid() if hasattr(os, "geteuid") else None
        if effective_uid is None or self.owner not in (0, effective_uid):
            raise _fail("launcher symlink owner is unsafe")
        _validate_directory_chain_shape(
            self.parent_chain,
            endpoint=os.path.dirname(self.path),
            label="launcher symlink parent binding",
        )

    def verify(self, ownership_root: str) -> None:
        if type(ownership_root) is not str:
            raise _fail("launcher ownership binding is invalid")
        try:
            metadata = os.lstat(self.path)
            target_text = os.readlink(self.path)
        except OSError:
            raise _fail("launcher symlink binding changed") from None
        if type(target_text) is not str:
            raise _fail("launcher symlink binding changed")
        resolved = _resolve_link_target(self.path, target_text)
        expected = (
            self.device,
            self.inode,
            self.owner,
            self.mode,
            self.size,
            self.mtime_ns,
            self.ctime_ns,
        )
        actual = (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_uid),
            stat.S_IMODE(metadata.st_mode),
            int(metadata.st_size),
            int(getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1e9))),
            int(getattr(metadata, "st_ctime_ns", int(metadata.st_ctime * 1e9))),
        )
        if (
            not stat.S_ISLNK(metadata.st_mode)
            or target_text != self.target_text
            or resolved != self.resolved_target
            or expected != actual
            or not _within(resolved, ownership_root)
            or _directory_chain(os.path.dirname(self.path)) != self.parent_chain
        ):
            raise _fail("launcher symlink binding changed")


def _resolve_link_target(path: str, target: str) -> str:
    _safe_text(target, label="launcher symlink target", maximum=_MAX_PATH_BYTES)
    if os.path.isabs(target):
        result = os.path.normpath(target)
    else:
        result = os.path.normpath(os.path.join(os.path.dirname(path), target))
    if not os.path.isabs(result) or len(os.fsencode(result)) > _MAX_PATH_BYTES:
        raise _fail("launcher symlink target is invalid")
    return result


def _capture_symlink_chain(
    invoked_path: str, ownership_root: str
) -> Tuple[Tuple[SymlinkIdentityV1, ...], str]:
    current = invoked_path
    seen = set()
    records = []
    for _ in range(_MAX_SYMLINKS + 1):
        if current in seen:
            raise _fail("launcher symlink chain is recursive")
        seen.add(current)
        if not _within(current, ownership_root):
            raise _fail("launcher symlink escapes installation ownership")
        parent_chain = _directory_chain(os.path.dirname(current))
        try:
            metadata = os.lstat(current)
        except OSError:
            raise _fail("launcher symlink chain could not be inspected") from None
        if not stat.S_ISLNK(metadata.st_mode):
            try:
                canonical = _absolute_path(
                    current, label="launcher target", canonical=True
                )
            except ConfigurationError:
                raise _fail("launcher target must be canonical")
            return tuple(records), canonical
        if len(records) >= _MAX_SYMLINKS:
            raise _fail("launcher symlink chain exceeds maximum depth")
        if metadata.st_uid not in (0, os.geteuid()):
            raise _fail("launcher symlink owner is unsafe")
        try:
            target_text = os.readlink(current)
        except OSError:
            raise _fail("launcher symlink chain could not be inspected") from None
        if type(target_text) is not str:
            raise _fail("launcher symlink target is invalid")
        resolved = _resolve_link_target(current, target_text)
        if not _within(resolved, ownership_root):
            raise _fail("launcher symlink escapes installation ownership")
        records.append(
            SymlinkIdentityV1(
                path=current,
                target_text=target_text,
                resolved_target=resolved,
                device=int(metadata.st_dev),
                inode=int(metadata.st_ino),
                owner=int(metadata.st_uid),
                mode=stat.S_IMODE(metadata.st_mode),
                size=int(metadata.st_size),
                mtime_ns=int(
                    getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1e9))
                ),
                ctime_ns=int(
                    getattr(metadata, "st_ctime_ns", int(metadata.st_ctime * 1e9))
                ),
                parent_chain=parent_chain,
            )
        )
        current = resolved
    raise _fail("launcher symlink chain exceeds maximum depth")


def _verify_symlink_chain(
    invoked_path: str,
    ownership_root: str,
    records: Tuple[SymlinkIdentityV1, ...],
    expected_target: str,
) -> None:
    if type(records) is not tuple or len(records) > _MAX_SYMLINKS:
        raise _fail("launcher symlink binding is invalid")
    current = invoked_path
    for record in records:
        if type(record) is not SymlinkIdentityV1 or record.path != current:
            raise _fail("launcher symlink binding changed")
        record.verify(ownership_root)
        current = record.resolved_target
    try:
        final_metadata = os.lstat(current)
    except OSError:
        raise _fail("launcher target binding changed") from None
    try:
        canonical = _absolute_path(current, label="launcher target", canonical=True)
    except ConfigurationError:
        raise _fail("launcher target binding changed") from None
    if (
        stat.S_ISLNK(final_metadata.st_mode)
        or current != expected_target
        or canonical != current
    ):
        raise _fail("launcher target binding changed")


def _shebang_tokens(header: bytes) -> Optional[Tuple[str, ...]]:
    if not header.startswith(b"#!"):
        return None
    first_line = header.split(b"\n", 1)[0]
    if len(first_line) >= _MAX_SHEBANG_BYTES:
        raise _fail("package launcher shebang is invalid")
    try:
        text = first_line[2:].decode("utf-8", "strict").strip()
    except UnicodeError:
        raise _fail("package launcher shebang is invalid") from None
    if not text:
        raise _fail("package launcher shebang is invalid")
    tokens = tuple(text.split())
    if len(tokens) > 2:
        raise _fail("package launcher shebang dispatch is unsupported")
    return tokens


def _is_native_executable(header: bytes) -> bool:
    """Recognize bounded platform binary headers before issuing direct argv."""

    if header.startswith(b"\x7fELF") or header.startswith(b"MZ"):
        return True
    return header[:4] in {
        b"\xfe\xed\xfa\xce",
        b"\xfe\xed\xfa\xcf",
        b"\xce\xfa\xed\xfe",
        b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf",
        b"\xbf\xba\xfe\xca",
    }


def _verify_manifest_content(
    identity: ArtifactIdentityV1,
    *,
    package_name: str,
    version: str,
    executable: str,
    bin_path: str,
) -> None:
    current, content = _capture_artifact(
        identity.path,
        maximum_bytes=_MAX_MANIFEST_BYTES,
        executable=False,
        retain_content=True,
    )
    if current != identity:
        raise _fail("package manifest binding changed")
    manifest = _parse_manifest(content)
    if _manifest_bin_path(
        manifest,
        package_name=package_name,
        version=version,
        executable=executable,
    ) != bin_path:
        raise _fail("package manifest binding changed")


def _artifact_matches_executable(
    artifact: ArtifactIdentityV1, executable: ExecutableIdentity
) -> bool:
    return (
        artifact.path,
        artifact.sha256,
        artifact.device,
        artifact.inode,
        artifact.size,
        artifact.mtime_ns,
        artifact.ctime_ns,
        artifact.mode,
        artifact.owner,
        tuple(
            (item.path, item.device, item.inode, item.owner, item.mode)
            for item in artifact.parent_chain
        ),
    ) == (
        executable.path,
        executable.sha256,
        executable.device,
        executable.inode,
        executable.size,
        executable.mtime_ns,
        executable.ctime_ns,
        executable.mode,
        executable.owner,
        executable.parent_chain,
    )


def _consume_receipt_text_budget(values: Tuple[Any, ...]) -> None:
    remaining = _MAX_RECEIPT_UTF8_BYTES
    active = set()

    def consume(value: Any, depth: int) -> None:
        nonlocal remaining
        if depth > 32:
            raise _fail("installation receipt metadata is too deeply nested")
        if type(value) is str:
            try:
                remaining -= len(value.encode("utf-8", "strict"))
            except UnicodeError:
                raise _fail("installation receipt metadata is invalid") from None
            if remaining < 0:
                raise _fail("installation receipt metadata is too large")
            return
        if value is None or type(value) in (int, bool):
            return
        if isinstance(value, Enum):
            consume(value.value, depth + 1)
            return
        marker = id(value)
        if marker in active:
            raise _fail("installation receipt metadata is recursive")
        active.add(marker)
        try:
            if type(value) is tuple:
                for item in value:
                    consume(item, depth + 1)
            elif type(value) is DirectoryIdentityV1:
                consume(value.path, depth + 1)
            elif type(value) is ArtifactIdentityV1:
                consume(value.path, depth + 1)
                consume(value.sha256, depth + 1)
                consume(value.parent_chain, depth + 1)
            elif type(value) is SymlinkIdentityV1:
                consume(value.path, depth + 1)
                consume(value.target_text, depth + 1)
                consume(value.resolved_target, depth + 1)
                consume(value.parent_chain, depth + 1)
            elif type(value) is ExecutableIdentity:
                consume(value.path, depth + 1)
                consume(value.sha256, depth + 1)
                consume(value.parent_chain, depth + 1)
                consume(value.interpreter, depth + 1)
            else:
                raise _fail("installation receipt metadata is invalid")
        finally:
            active.remove(marker)

    consume(values, 0)


@dataclass(frozen=True)
class VerifiedLaunchV1:
    """Canonical launch prefix and identities for every executable path."""

    provider_id: str
    receipt_kind: InstallationReceiptKindV1
    argv_prefix: Tuple[str, ...]
    executable_identity: ExecutableIdentity
    abi_version: int = INSTALLATION_RECEIPT_ABI_V1
    # Appended with a default so existing direct-executable v1 construction
    # retains its positional field mapping. Multi-path receipts always supply
    # the complete tuple from ``InstallationReceiptV1.verify``.
    launch_identities: Tuple[ExecutableIdentity, ...] = ()

    def __post_init__(self) -> None:
        _provider_id(self.provider_id)
        if type(self.receipt_kind) is not InstallationReceiptKindV1:
            raise _fail("verified launch receipt kind is invalid")
        if type(self.abi_version) is not int or self.abi_version != INSTALLATION_RECEIPT_ABI_V1:
            raise _fail("verified launch ABI is invalid")
        if type(self.argv_prefix) is not tuple or not 1 <= len(self.argv_prefix) <= 4:
            raise _fail("verified launch argv prefix is invalid")
        for item in self.argv_prefix:
            _absolute_path(item, label="verified launch argv entry", canonical=True)
        _validate_executable_identity_shape(self.executable_identity)
        if self.argv_prefix[0] != self.executable_identity.path:
            raise _fail("verified launch executable identity does not match argv")
        if not self.launch_identities and len(self.argv_prefix) == 1:
            object.__setattr__(
                self, "launch_identities", (self.executable_identity,)
            )
        if (
            type(self.launch_identities) is not tuple
            or len(self.launch_identities) != len(self.argv_prefix)
            or not self.launch_identities
        ):
            raise _fail("verified launch identities do not match argv")
        for index, identity in enumerate(self.launch_identities):
            _validate_executable_identity_shape(identity)
            if identity.path != self.argv_prefix[index]:
                raise _fail("verified launch identities do not match argv")
        if self.launch_identities[0] != self.executable_identity:
            raise _fail("verified launch executable identity changed")
        _consume_receipt_text_budget(
            (
                self.provider_id,
                self.receipt_kind,
                self.argv_prefix,
                self.executable_identity,
                self.launch_identities,
            )
        )


@dataclass(frozen=True)
class InstallationReceiptV1:
    """Immutable, locally verified launch binding for one acquisition."""

    receipt_kind: InstallationReceiptKindV1
    distribution_type: DistributionTypeV1
    provider_id: str
    executable_basename: str
    distribution_name: str
    distribution_version: str
    acquisition_source: str
    acquisition_url: Optional[str]
    invoked_launcher_path: str
    canonical_launch_target: str
    argv_prefix: Tuple[str, ...]
    executable_identity: ExecutableIdentity
    target_identity: ArtifactIdentityV1
    interpreter_identity: Optional[ExecutableIdentity]
    ownership_root: Optional[str]
    package_root: Optional[str]
    package_manifest_identity: Optional[ArtifactIdentityV1]
    package_manifest_bin: Optional[str]
    symlink_chain: Tuple[SymlinkIdentityV1, ...]
    ownership_chain: Tuple[DirectoryIdentityV1, ...]
    package_root_chain: Tuple[DirectoryIdentityV1, ...]
    abi_version: int = INSTALLATION_RECEIPT_ABI_V1

    def __post_init__(self) -> None:
        if type(self.receipt_kind) is not InstallationReceiptKindV1:
            raise _fail("installation receipt kind is invalid")
        if type(self.distribution_type) is not DistributionTypeV1:
            raise _fail("installation distribution type is invalid")
        if type(self.abi_version) is not int or self.abi_version != INSTALLATION_RECEIPT_ABI_V1:
            raise _fail("installation receipt ABI is invalid")
        _provider_id(self.provider_id)
        _basename(self.executable_basename)
        _safe_text(self.distribution_name, label="distribution name", maximum=256)
        _version(self.distribution_version)
        _safe_text(self.acquisition_source, label="acquisition source", maximum=1024)
        if self.acquisition_url is not None:
            _safe_text(self.acquisition_url, label="acquisition URL", maximum=4096)
        _absolute_path(
            self.invoked_launcher_path,
            label="invoked launcher path",
            canonical=self.receipt_kind is InstallationReceiptKindV1.DIRECT_EXECUTABLE,
        )
        _absolute_path(
            self.canonical_launch_target,
            label="canonical launch target",
            canonical=True,
        )
        if os.path.basename(self.invoked_launcher_path) != self.executable_basename:
            raise _fail("invoked launcher basename does not match receipt")
        if type(self.argv_prefix) is not tuple or not 1 <= len(self.argv_prefix) <= 2:
            raise _fail("installation argv prefix is invalid")
        for item in self.argv_prefix:
            _absolute_path(item, label="installation argv entry", canonical=True)
        _validate_executable_identity_shape(self.executable_identity)
        if type(self.target_identity) is not ArtifactIdentityV1:
            raise _fail("installation target identity is invalid")
        if self.interpreter_identity is not None:
            _validate_executable_identity_shape(self.interpreter_identity)
        if self.argv_prefix[0] != self.executable_identity.path:
            raise _fail("installation executable identity does not match argv")
        if self.target_identity.path != self.canonical_launch_target:
            raise _fail("installation target identity does not match launcher")
        for collection, item_type, label, maximum in (
            (self.symlink_chain, SymlinkIdentityV1, "symlink", _MAX_SYMLINKS),
            (self.ownership_chain, DirectoryIdentityV1, "ownership", 1024),
            (self.package_root_chain, DirectoryIdentityV1, "package", 1024),
        ):
            if type(collection) is not tuple or len(collection) > maximum:
                raise _fail("installation {} binding is invalid".format(label))
            if any(type(item) is not item_type for item in collection):
                raise _fail("installation {} binding is invalid".format(label))
        if self.receipt_kind is InstallationReceiptKindV1.DIRECT_EXECUTABLE:
            if self.distribution_type is not DistributionTypeV1.VENDOR_EXECUTABLE:
                raise _fail("direct receipt distribution type is invalid")
            if (
                self.invoked_launcher_path != self.canonical_launch_target
                or self.argv_prefix != (self.canonical_launch_target,)
                or self.ownership_root is not None
                or self.package_root is not None
                or self.package_manifest_identity is not None
                or self.package_manifest_bin is not None
                or self.symlink_chain
                or self.ownership_chain
                or self.package_root_chain
                or self.interpreter_identity != self.executable_identity.interpreter
                or not _artifact_matches_executable(
                    self.target_identity, self.executable_identity
                )
            ):
                raise _fail("direct installation receipt is inconsistent")
        else:
            if self.distribution_type is not DistributionTypeV1.NPM_PACKAGE:
                raise _fail("npm receipt distribution type is invalid")
            if (
                self.ownership_root is None
                or self.package_root is None
                or self.package_manifest_identity is None
                or self.package_manifest_bin is None
                or not self.ownership_chain
                or not self.package_root_chain
            ):
                raise _fail("npm installation receipt is incomplete")
            _absolute_path(self.ownership_root, label="npm ownership root", canonical=True)
            _absolute_path(self.package_root, label="npm package root", canonical=True)
            manifest_bin = _safe_text(
                self.package_manifest_bin,
                label="package manifest launcher",
                maximum=4096,
            )
            if (
                self.ownership_root == os.path.sep
                or self.package_root == self.ownership_root
                or not _within(self.invoked_launcher_path, self.ownership_root)
                or not _within(self.package_root, self.ownership_root)
                or not _within(self.canonical_launch_target, self.package_root)
                or self.package_manifest_identity.path
                != os.path.join(self.package_root, "package.json")
                or self.package_manifest_identity.size > _MAX_MANIFEST_BYTES
                or os.path.isabs(manifest_bin)
                or os.path.normpath(manifest_bin) != manifest_bin
                or os.path.normpath(os.path.join(self.package_root, manifest_bin))
                != self.canonical_launch_target
            ):
                raise _fail("npm installation receipt binding is invalid")
            _validate_directory_chain_shape(
                self.ownership_chain,
                endpoint=self.ownership_root,
                label="npm ownership binding",
            )
            _validate_directory_chain_shape(
                self.package_root_chain,
                endpoint=self.package_root,
                label="npm package binding",
            )
            if self.interpreter_identity is None:
                if (
                    self.argv_prefix != (self.canonical_launch_target,)
                    or not _artifact_matches_executable(
                        self.target_identity, self.executable_identity
                    )
                ):
                    raise _fail("native npm launch binding is inconsistent")
            elif (
                self.interpreter_identity != self.executable_identity
                or self.argv_prefix
                != (self.interpreter_identity.path, self.canonical_launch_target)
            ):
                raise _fail("interpreted npm launch binding is inconsistent")
            current = self.invoked_launcher_path
            for record in self.symlink_chain:
                if record.path != current or not _within(
                    record.resolved_target, self.ownership_root
                ):
                    raise _fail("npm launcher symlink binding is invalid")
                current = record.resolved_target
            if current != self.canonical_launch_target:
                raise _fail("npm launcher target binding is invalid")
        _consume_receipt_text_budget(
            (
                self.receipt_kind,
                self.distribution_type,
                self.provider_id,
                self.executable_basename,
                self.distribution_name,
                self.distribution_version,
                self.acquisition_source,
                self.acquisition_url,
                self.invoked_launcher_path,
                self.canonical_launch_target,
                self.argv_prefix,
                self.executable_identity,
                self.target_identity,
                self.interpreter_identity,
                self.ownership_root,
                self.package_root,
                self.package_manifest_identity,
                self.package_manifest_bin,
                self.symlink_chain,
                self.ownership_chain,
                self.package_root_chain,
            )
        )

    @classmethod
    def capture_direct(
        cls,
        *,
        provider_id: str,
        executable_path: str,
        executable_basename: str,
        distribution_name: str,
        distribution_version: str,
        acquisition_source: str,
        acquisition_url: Optional[str] = None,
    ) -> "InstallationReceiptV1":
        provider = _provider_id(provider_id)
        basename = _basename(executable_basename)
        name = _safe_text(distribution_name, label="distribution name", maximum=256)
        version = _version(distribution_version)
        source = _safe_text(acquisition_source, label="acquisition source", maximum=1024)
        if acquisition_url is not None:
            _safe_text(acquisition_url, label="acquisition URL", maximum=4096)
        path = _absolute_path(executable_path, label="direct executable path", canonical=True)
        if os.path.basename(path) != basename:
            raise _fail("direct executable basename does not match receipt")
        identity = ExecutableIdentity.capture(path)
        return cls(
            receipt_kind=InstallationReceiptKindV1.DIRECT_EXECUTABLE,
            distribution_type=DistributionTypeV1.VENDOR_EXECUTABLE,
            provider_id=provider,
            executable_basename=basename,
            distribution_name=name,
            distribution_version=version,
            acquisition_source=source,
            acquisition_url=acquisition_url,
            invoked_launcher_path=path,
            canonical_launch_target=path,
            argv_prefix=(path,),
            executable_identity=identity,
            target_identity=_artifact_from_executable(identity),
            interpreter_identity=identity.interpreter,
            ownership_root=None,
            package_root=None,
            package_manifest_identity=None,
            package_manifest_bin=None,
            symlink_chain=(),
            ownership_chain=(),
            package_root_chain=(),
        )

    capture_direct_executable = capture_direct

    @classmethod
    def capture_explicit_direct(
        cls,
        *,
        provider_id: str,
        executable_path: str,
        executable_basename: str,
    ) -> "InstallationReceiptV1":
        """Capture a caller-selected canonical binary without invented provenance."""

        return cls.capture_direct(
            provider_id=provider_id,
            executable_path=executable_path,
            executable_basename=executable_basename,
            distribution_name=provider_id,
            distribution_version="0.0.0+explicit",
            acquisition_source="explicit-local-path",
            acquisition_url=None,
        )

    @classmethod
    def capture_npm(
        cls,
        *,
        provider_id: str,
        launcher_path: str,
        executable_basename: str,
        package_root: str,
        ownership_root: str,
        distribution_name: str,
        distribution_version: str,
        acquisition_source: str,
        acquisition_url: Optional[str] = None,
        interpreter_path: Optional[str] = None,
    ) -> "InstallationReceiptV1":
        provider = _provider_id(provider_id)
        basename = _basename(executable_basename)
        package_name = _safe_text(
            distribution_name, label="npm package name", maximum=255
        )
        if _NPM_NAME_RE.fullmatch(package_name) is None:
            raise _fail("npm package name is invalid")
        version = _version(distribution_version)
        source = _safe_text(acquisition_source, label="acquisition source", maximum=1024)
        if acquisition_url is not None:
            _safe_text(acquisition_url, label="acquisition URL", maximum=4096)
        invoked = _absolute_path(launcher_path, label="npm launcher path", canonical=False)
        if os.path.basename(invoked) != basename:
            raise _fail("npm launcher basename does not match receipt")
        boundary = _absolute_path(
            ownership_root, label="npm ownership root", canonical=True
        )
        package = _absolute_path(package_root, label="npm package root", canonical=True)
        if (
            boundary == os.path.sep
            or package == boundary
            or not _within(invoked, boundary)
            or not _within(package, boundary)
        ):
            raise _fail("npm installation is outside its ownership root")
        ownership_chain = _directory_chain(boundary)
        package_chain = _directory_chain(package)
        symlinks, resolved_launcher = _capture_symlink_chain(invoked, boundary)

        manifest_path = os.path.join(package, "package.json")
        manifest_identity, manifest_content = _capture_artifact(
            manifest_path,
            maximum_bytes=_MAX_MANIFEST_BYTES,
            executable=False,
            retain_content=True,
        )
        manifest = _parse_manifest(manifest_content)
        bin_path = _manifest_bin_path(
            manifest,
            package_name=package_name,
            version=version,
            executable=basename,
        )
        expected_target = os.path.normpath(os.path.join(package, bin_path))
        if not _within(expected_target, package):
            raise _fail("package launcher target is outside the canonical package root")
        try:
            expected_target = _absolute_path(
                expected_target, label="package launcher target", canonical=True
            )
        except ConfigurationError:
            raise _fail(
                "package launcher target is outside the canonical package root"
            ) from None
        if resolved_launcher != expected_target:
            raise _fail("npm launcher does not resolve to the package manifest target")

        target_identity, header = _capture_artifact(
            expected_target,
            maximum_bytes=_MAX_EXECUTABLE_BYTES,
            executable=True,
            retain_content=False,
        )
        shebang = _shebang_tokens(header)
        interpreter_identity = None  # type: Optional[ExecutableIdentity]
        if shebang is None:
            if interpreter_path is not None:
                raise _fail("native package launcher must not specify an interpreter")
            if not _is_native_executable(header):
                raise _fail("package launcher format is unsupported")
            executable_identity = _executable_from_artifact(target_identity)
            argv_prefix = (expected_target,)
        else:
            if type(interpreter_path) is not str:
                raise _fail("package launcher requires an explicit interpreter path")
            interpreter = _absolute_path(
                interpreter_path,
                label="package launcher interpreter path",
                canonical=True,
            )
            command = shebang[0]
            if not os.path.isabs(command):
                raise _fail("package launcher shebang dispatch is unsupported")
            if os.path.basename(command) == "env":
                if len(shebang) != 2 or shebang[1] != os.path.basename(interpreter):
                    raise _fail("package launcher shebang does not match interpreter")
            elif len(shebang) != 1 or command != interpreter:
                raise _fail("package launcher shebang does not match interpreter")
            interpreter_identity = ExecutableIdentity.capture(interpreter)
            executable_identity = interpreter_identity
            argv_prefix = (interpreter, expected_target)

        _verify_directory_chain(boundary, ownership_chain)
        _verify_directory_chain(package, package_chain)
        _verify_symlink_chain(invoked, boundary, symlinks, expected_target)
        manifest_identity.verify_metadata(executable=False)
        target_identity.verify_metadata(executable=True)
        if interpreter_identity is not None:
            interpreter_identity.verify_metadata()
        return cls(
            receipt_kind=InstallationReceiptKindV1.NPM_PACKAGE_LAUNCHER,
            distribution_type=DistributionTypeV1.NPM_PACKAGE,
            provider_id=provider,
            executable_basename=basename,
            distribution_name=package_name,
            distribution_version=version,
            acquisition_source=source,
            acquisition_url=acquisition_url,
            invoked_launcher_path=invoked,
            canonical_launch_target=expected_target,
            argv_prefix=argv_prefix,
            executable_identity=executable_identity,
            target_identity=target_identity,
            interpreter_identity=interpreter_identity,
            ownership_root=boundary,
            package_root=package,
            package_manifest_identity=manifest_identity,
            package_manifest_bin=bin_path,
            symlink_chain=symlinks,
            ownership_chain=ownership_chain,
            package_root_chain=package_chain,
        )

    capture_npm_package = capture_npm

    def verify(self) -> VerifiedLaunchV1:
        """Re-check the complete binding and return an immutable launch prefix."""

        if type(self) is not InstallationReceiptV1:
            raise _fail("installation receipt type is invalid")
        # Re-run structural validation so forged/deserialized objects fail closed.
        self.__post_init__()
        if self.receipt_kind is InstallationReceiptKindV1.DIRECT_EXECUTABLE:
            if os.path.basename(self.canonical_launch_target) != self.executable_basename:
                raise _fail("direct executable binding changed")
            self.executable_identity.verify_metadata()
            self.target_identity.verify_metadata(executable=True)
        else:
            ownership_root = self.ownership_root
            package_root = self.package_root
            manifest_identity = self.package_manifest_identity
            manifest_bin = self.package_manifest_bin
            if (
                type(ownership_root) is not str
                or type(package_root) is not str
                or type(manifest_identity) is not ArtifactIdentityV1
                or type(manifest_bin) is not str
            ):
                raise _fail("npm installation receipt is incomplete")
            _verify_directory_chain(ownership_root, self.ownership_chain)
            _verify_directory_chain(package_root, self.package_root_chain)
            _verify_symlink_chain(
                self.invoked_launcher_path,
                ownership_root,
                self.symlink_chain,
                self.canonical_launch_target,
            )
            _verify_manifest_content(
                manifest_identity,
                package_name=self.distribution_name,
                version=self.distribution_version,
                executable=self.executable_basename,
                bin_path=manifest_bin,
            )
            self.target_identity.verify_metadata(executable=True)
            self.executable_identity.verify_metadata()
            if self.interpreter_identity is not None:
                if self.interpreter_identity != self.executable_identity:
                    raise _fail("package interpreter binding is inconsistent")
                self.interpreter_identity.verify_metadata()
        return VerifiedLaunchV1(
            provider_id=self.provider_id,
            receipt_kind=self.receipt_kind,
            argv_prefix=self.argv_prefix,
            executable_identity=self.executable_identity,
            launch_identities=(
                (self.executable_identity,)
                if len(self.argv_prefix) == 1
                else (
                    self.executable_identity,
                    _executable_from_artifact(self.target_identity),
                )
            ),
        )


_RECEIPT_RECORD_SCHEMA_V1 = 1
_RECEIPT_RECORD_KEYS = frozenset(
    {
        "schema",
        "abi_version",
        "receipt_kind",
        "distribution_type",
        "provider_id",
        "executable_basename",
        "distribution_name",
        "distribution_version",
        "acquisition_source",
        "acquisition_url",
        "invoked_launcher_path",
        "canonical_launch_target",
        "argv_prefix",
        "executable_identity",
        "target_identity",
        "interpreter_identity",
        "ownership_root",
        "package_root",
        "package_manifest_identity",
        "package_manifest_bin",
        "symlink_chain",
        "ownership_chain",
        "package_root_chain",
    }
)


def _exact_record(value: object, keys: frozenset, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail("{} is invalid".format(label))
    try:
        actual = frozenset(value.keys())
    except BaseException:
        raise _fail("{} is invalid".format(label)) from None
    if actual != keys or any(type(key) is not str for key in actual):
        raise _fail("{} is invalid".format(label))
    return value


def _directory_to_record(value: DirectoryIdentityV1) -> Dict[str, Any]:
    if type(value) is not DirectoryIdentityV1:
        raise _fail("directory receipt identity is invalid")
    return {
        "path": value.path,
        "device": value.device,
        "inode": value.inode,
        "owner": value.owner,
        "mode": value.mode,
    }


def _directory_from_record(value: object) -> DirectoryIdentityV1:
    record = _exact_record(
        value, frozenset({"path", "device", "inode", "owner", "mode"}),
        "directory receipt identity",
    )
    return DirectoryIdentityV1(
        path=record["path"],
        device=record["device"],
        inode=record["inode"],
        owner=record["owner"],
        mode=record["mode"],
    )


def _directory_chain_to_record(
    values: Tuple[DirectoryIdentityV1, ...]
) -> list:
    if type(values) is not tuple:
        raise _fail("directory receipt chain is invalid")
    return [_directory_to_record(value) for value in values]


def _directory_chain_from_record(value: object) -> Tuple[DirectoryIdentityV1, ...]:
    if type(value) not in (list, tuple) or len(value) > 1024:
        raise _fail("directory receipt chain is invalid")
    return tuple(_directory_from_record(item) for item in value)


def _executable_to_record(value: ExecutableIdentity) -> Dict[str, Any]:
    if type(value) is not ExecutableIdentity:
        raise _fail("executable receipt identity is invalid")
    return {
        "path": value.path,
        "sha256": value.sha256,
        "device": value.device,
        "inode": value.inode,
        "size": value.size,
        "mtime_ns": value.mtime_ns,
        "ctime_ns": value.ctime_ns,
        "mode": value.mode,
        "owner": value.owner,
        "parent_chain": [list(item) for item in value.parent_chain],
        "interpreter": (
            None
            if value.interpreter is None
            else _executable_to_record(value.interpreter)
        ),
    }


def _executable_from_record(value: object, depth: int = 0) -> ExecutableIdentity:
    if depth > 8:
        raise _fail("executable receipt identity is too deeply nested")
    record = _exact_record(
        value,
        frozenset(
            {
                "path", "sha256", "device", "inode", "size", "mtime_ns",
                "ctime_ns", "mode", "owner", "parent_chain", "interpreter",
            }
        ),
        "executable receipt identity",
    )
    parent_chain_value = record["parent_chain"]
    if type(parent_chain_value) not in (list, tuple) or len(parent_chain_value) > 1024:
        raise _fail("executable receipt parent chain is invalid")
    parent_chain = []
    for item in parent_chain_value:
        if type(item) not in (list, tuple) or len(item) != 5:
            raise _fail("executable receipt parent chain is invalid")
        parent_chain.append(tuple(item))
    interpreter_value = record["interpreter"]
    interpreter = (
        None
        if interpreter_value is None
        else _executable_from_record(interpreter_value, depth + 1)
    )
    return ExecutableIdentity(
        path=record["path"],
        sha256=record["sha256"],
        device=record["device"],
        inode=record["inode"],
        size=record["size"],
        mtime_ns=record["mtime_ns"],
        mode=record["mode"],
        owner=record["owner"],
        parent_chain=tuple(parent_chain),
        interpreter=interpreter,
        ctime_ns=record["ctime_ns"],
    )


def _artifact_to_record(value: ArtifactIdentityV1) -> Dict[str, Any]:
    if type(value) is not ArtifactIdentityV1:
        raise _fail("artifact receipt identity is invalid")
    return {
        "path": value.path,
        "sha256": value.sha256,
        "device": value.device,
        "inode": value.inode,
        "size": value.size,
        "mtime_ns": value.mtime_ns,
        "ctime_ns": value.ctime_ns,
        "mode": value.mode,
        "owner": value.owner,
        "parent_chain": _directory_chain_to_record(value.parent_chain),
    }


def _artifact_from_record(value: object) -> ArtifactIdentityV1:
    record = _exact_record(
        value,
        frozenset(
            {
                "path", "sha256", "device", "inode", "size", "mtime_ns",
                "ctime_ns", "mode", "owner", "parent_chain",
            }
        ),
        "artifact receipt identity",
    )
    return ArtifactIdentityV1(
        path=record["path"],
        sha256=record["sha256"],
        device=record["device"],
        inode=record["inode"],
        size=record["size"],
        mtime_ns=record["mtime_ns"],
        ctime_ns=record["ctime_ns"],
        mode=record["mode"],
        owner=record["owner"],
        parent_chain=_directory_chain_from_record(record["parent_chain"]),
    )


def _symlink_to_record(value: SymlinkIdentityV1) -> Dict[str, Any]:
    if type(value) is not SymlinkIdentityV1:
        raise _fail("symlink receipt identity is invalid")
    return {
        "path": value.path,
        "target_text": value.target_text,
        "resolved_target": value.resolved_target,
        "device": value.device,
        "inode": value.inode,
        "owner": value.owner,
        "mode": value.mode,
        "size": value.size,
        "mtime_ns": value.mtime_ns,
        "ctime_ns": value.ctime_ns,
        "parent_chain": _directory_chain_to_record(value.parent_chain),
    }


def _symlink_from_record(value: object) -> SymlinkIdentityV1:
    record = _exact_record(
        value,
        frozenset(
            {
                "path", "target_text", "resolved_target", "device", "inode",
                "owner", "mode", "size", "mtime_ns", "ctime_ns", "parent_chain",
            }
        ),
        "symlink receipt identity",
    )
    return SymlinkIdentityV1(
        path=record["path"],
        target_text=record["target_text"],
        resolved_target=record["resolved_target"],
        device=record["device"],
        inode=record["inode"],
        owner=record["owner"],
        mode=record["mode"],
        size=record["size"],
        mtime_ns=record["mtime_ns"],
        ctime_ns=record["ctime_ns"],
        parent_chain=_directory_chain_from_record(record["parent_chain"]),
    )


def installation_receipt_to_record(
    receipt: InstallationReceiptV1, *, persistent: bool = False
) -> Dict[str, Any]:
    """Serialize one exact receipt without executable code or class metadata."""

    if type(receipt) is not InstallationReceiptV1:
        raise _fail("installation receipt type is invalid")
    receipt.verify()
    return {
        "schema": _RECEIPT_RECORD_SCHEMA_V1,
        "abi_version": receipt.abi_version,
        "receipt_kind": receipt.receipt_kind.value,
        "distribution_type": receipt.distribution_type.value,
        "provider_id": receipt.provider_id,
        "executable_basename": receipt.executable_basename,
        "distribution_name": receipt.distribution_name,
        "distribution_version": receipt.distribution_version,
        "acquisition_source": (
            "configured-local-installation"
            if persistent else receipt.acquisition_source
        ),
        # URLs are informational and may contain userinfo/query credentials.
        "acquisition_url": None if persistent else receipt.acquisition_url,
        "invoked_launcher_path": receipt.invoked_launcher_path,
        "canonical_launch_target": receipt.canonical_launch_target,
        "argv_prefix": list(receipt.argv_prefix),
        "executable_identity": _executable_to_record(receipt.executable_identity),
        "target_identity": _artifact_to_record(receipt.target_identity),
        "interpreter_identity": (
            None
            if receipt.interpreter_identity is None
            else _executable_to_record(receipt.interpreter_identity)
        ),
        "ownership_root": receipt.ownership_root,
        "package_root": receipt.package_root,
        "package_manifest_identity": (
            None
            if receipt.package_manifest_identity is None
            else _artifact_to_record(receipt.package_manifest_identity)
        ),
        "package_manifest_bin": receipt.package_manifest_bin,
        "symlink_chain": [_symlink_to_record(item) for item in receipt.symlink_chain],
        "ownership_chain": _directory_chain_to_record(receipt.ownership_chain),
        "package_root_chain": _directory_chain_to_record(receipt.package_root_chain),
    }


def installation_receipt_from_record(value: Mapping[str, Any]) -> InstallationReceiptV1:
    """Reconstruct and reverify one strict, hand-written receipt record."""

    record = _exact_record(value, _RECEIPT_RECORD_KEYS, "installation receipt record")
    if (
        type(record["schema"]) is not int
        or record["schema"] != _RECEIPT_RECORD_SCHEMA_V1
    ):
        raise _fail("installation receipt record schema is unsupported")
    try:
        receipt_kind = InstallationReceiptKindV1(record["receipt_kind"])
        distribution_type = DistributionTypeV1(record["distribution_type"])
    except (TypeError, ValueError):
        raise _fail("installation receipt record enum is invalid") from None
    argv_value = record["argv_prefix"]
    symlink_value = record["symlink_chain"]
    if type(argv_value) not in (list, tuple) or type(symlink_value) not in (list, tuple):
        raise _fail("installation receipt record collection is invalid")
    interpreter_value = record["interpreter_identity"]
    manifest_value = record["package_manifest_identity"]
    receipt = InstallationReceiptV1(
        receipt_kind=receipt_kind,
        distribution_type=distribution_type,
        provider_id=record["provider_id"],
        executable_basename=record["executable_basename"],
        distribution_name=record["distribution_name"],
        distribution_version=record["distribution_version"],
        acquisition_source=record["acquisition_source"],
        acquisition_url=record["acquisition_url"],
        invoked_launcher_path=record["invoked_launcher_path"],
        canonical_launch_target=record["canonical_launch_target"],
        argv_prefix=tuple(argv_value),
        executable_identity=_executable_from_record(record["executable_identity"]),
        target_identity=_artifact_from_record(record["target_identity"]),
        interpreter_identity=(
            None
            if interpreter_value is None
            else _executable_from_record(interpreter_value)
        ),
        ownership_root=record["ownership_root"],
        package_root=record["package_root"],
        package_manifest_identity=(
            None if manifest_value is None else _artifact_from_record(manifest_value)
        ),
        package_manifest_bin=record["package_manifest_bin"],
        symlink_chain=tuple(_symlink_from_record(item) for item in symlink_value),
        ownership_chain=_directory_chain_from_record(record["ownership_chain"]),
        package_root_chain=_directory_chain_from_record(record["package_root_chain"]),
        abi_version=record["abi_version"],
    )
    receipt.verify()
    return receipt


__all__ = [
    "INSTALLATION_RECEIPT_ABI_V1",
    "ArtifactIdentityV1",
    "DirectoryIdentityV1",
    "DistributionTypeV1",
    "InstallationReceiptKindV1",
    "InstallationReceiptV1",
    "SymlinkIdentityV1",
    "VerifiedLaunchV1",
    "installation_receipt_from_record",
    "installation_receipt_to_record",
]
