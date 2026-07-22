"""Typed, non-discovering launch configuration for extension providers.

Settings retain only a content digest and an optional provider-home path.  The
format-tagged receipt itself lives in a private, content-addressed file so it
does not consume or weaken the generic provider-settings JSON budget.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX import compatibility
    fcntl = None  # type: ignore[assignment]

from . import settings
from .plugin import (
    ProviderReceiptEnvelopeV1,
    _copy_provider_env,
    _valid_absolute_path,
    _valid_provider_id,
)


_STORE_SCHEMA_V1 = 1
_MAX_RECEIPT_FILE_BYTES = 2 * 1024 * 1024
_RECEIPT_PREFIX = "receipt-v1-"
_RECEIPT_SUFFIX = ".json"
_STORE_THREAD_LOCK = threading.RLock()


def _checked_path(value: Optional[str], label: str) -> Optional[str]:
    if value is None:
        return None
    if not _valid_absolute_path(value):
        raise ValueError("{} is invalid".format(label))
    return value


@dataclass(frozen=True)
class ExtensionLaunchOverridesV1:
    """Explicit, request-local provider launch overrides.

    Environment values are copied into an immutable Core-owned mapping and
    are never persisted by this module.
    """

    receipt: Optional[ProviderReceiptEnvelopeV1] = None
    bin_path: Optional[str] = None
    provider_home: Optional[str] = None
    extra_env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.receipt is not None and type(self.receipt) is not ProviderReceiptEnvelopeV1:
            raise TypeError("receipt must be ProviderReceiptEnvelopeV1")
        if self.receipt is not None and self.bin_path is not None:
            raise ValueError("receipt and bin_path are mutually exclusive")
        object.__setattr__(self, "bin_path", _checked_path(self.bin_path, "bin_path"))
        object.__setattr__(
            self,
            "provider_home",
            _checked_path(self.provider_home, "provider_home"),
        )
        object.__setattr__(self, "extra_env", _copy_provider_env(self.extra_env))


@dataclass(frozen=True)
class StoredExtensionLaunchV1:
    provider_id: str
    receipt_sha256: str
    provider_home: Optional[str]
    receipt: ProviderReceiptEnvelopeV1

    def __post_init__(self) -> None:
        if not _valid_provider_id(self.provider_id):
            raise ValueError("invalid provider id")
        if type(self.receipt) is not ProviderReceiptEnvelopeV1:
            raise TypeError("receipt must be ProviderReceiptEnvelopeV1")
        if self.receipt.provider_id != self.provider_id:
            raise ValueError("stored receipt provider mismatch")
        settings.ExtensionLaunchSettingsV1(
            provider_id=self.provider_id,
            receipt_sha256=self.receipt_sha256,
            provider_home=self.provider_home,
        )


def _require_provider_id(provider_id: object) -> str:
    if not _valid_provider_id(provider_id):
        raise ValueError("invalid extension provider id")
    return provider_id


def _owner_id() -> int:
    getuid = getattr(os, "getuid", None)
    return getuid() if callable(getuid) else -1


@contextmanager
def _store_lock() -> Iterator[None]:
    """Serialize receipt publication/load/clear across threads and processes."""

    with _STORE_THREAD_LOCK:
        settings._ensure_private_dir()
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(settings.SETTINGS_DIR / "providers.lock"), flags, 0o600)
        locked = False
        try:
            info = os.fstat(fd)
            owner = _owner_id()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or (owner >= 0 and info.st_uid != owner)
            ):
                raise OSError("provider configuration lock is unsafe")
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                if stat.S_IMODE(info.st_mode) & 0o077:
                    raise
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
                locked = True
            yield
        finally:
            if fcntl is not None and locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _validate_private_directory(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OSError("provider configuration directory must be a real directory")
    owner = _owner_id()
    if owner >= 0 and info.st_uid != owner:
        raise OSError("provider configuration directory has the wrong owner")
    try:
        os.chmod(str(path), 0o700, follow_symlinks=False)
    except OSError:
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    _validate_private_directory(path)


def _provider_directory(provider_id: str, *, create: bool) -> Path:
    provider_id = _require_provider_id(provider_id)
    root = settings.SETTINGS_DIR
    providers = root / "providers"
    provider = providers / provider_id
    if create:
        settings._ensure_private_dir()
        _ensure_private_directory(root)
        _ensure_private_directory(providers)
        _ensure_private_directory(provider)
    else:
        _validate_private_directory(root)
        _validate_private_directory(providers)
        _validate_private_directory(provider)
    return provider


def default_provider_home(provider_id: str) -> str:
    """Return a private default home path, creating only its safe parent."""

    return str(_provider_directory(provider_id, create=True) / "home")


def _receipt_path(provider_id: str, digest: str, *, create: bool) -> Path:
    metadata = settings.ExtensionLaunchSettingsV1(
        provider_id=_require_provider_id(provider_id),
        receipt_sha256=digest,
        provider_home=None,
    )
    return _provider_directory(provider_id, create=create) / (
        _RECEIPT_PREFIX + metadata.receipt_sha256 + _RECEIPT_SUFFIX
    )


def _plain_json(value: Any) -> Any:
    if value is None or type(value) in (bool, int, float, str):
        return value
    if type(value) in (list, tuple):
        return [_plain_json(item) for item in value]
    if not isinstance(value, Mapping):
        raise TypeError("receipt payload must contain JSON values")
    result: Dict[str, Any] = {}
    for key, item in value.items():
        if type(key) is not str:
            raise TypeError("receipt payload keys must be strings")
        result[key] = _plain_json(item)
    return result


def _encoded_envelope(envelope: ProviderReceiptEnvelopeV1) -> bytes:
    if type(envelope) is not ProviderReceiptEnvelopeV1:
        raise TypeError("receipt must be ProviderReceiptEnvelopeV1")
    # Reconstruction reruns all Core bounds even for an object forged with
    # object.__setattr__ before canonical serialization.
    checked = ProviderReceiptEnvelopeV1(
        provider_id=envelope.provider_id,
        media_type=envelope.media_type,
        payload=envelope.payload,
    )
    payload = {
        "schema": _STORE_SCHEMA_V1,
        "provider_id": checked.provider_id,
        "media_type": checked.media_type,
        "payload": _plain_json(checked.payload),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > _MAX_RECEIPT_FILE_BYTES:
        raise ValueError("provider receipt is too large")
    return encoded


def _reject_duplicate_pairs(pairs: list) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate receipt record key")
        result[key] = value
    return result


def _decoded_envelope(encoded: bytes) -> ProviderReceiptEnvelopeV1:
    if len(encoded) > _MAX_RECEIPT_FILE_BYTES:
        raise ValueError("provider receipt is too large")

    def reject_constant(_value: str) -> None:
        raise ValueError("provider receipt number is invalid")

    value = json.loads(
        encoded.decode("utf-8", "strict"),
        object_pairs_hook=_reject_duplicate_pairs,
        parse_constant=reject_constant,
    )
    if type(value) is not dict or set(value) != {
        "schema", "provider_id", "media_type", "payload",
    }:
        raise ValueError("provider receipt record is invalid")
    if (
        type(value["schema"]) is not int
        or value["schema"] != _STORE_SCHEMA_V1
        or type(value["payload"]) is not dict
    ):
        raise ValueError("provider receipt record schema is unsupported")
    return ProviderReceiptEnvelopeV1(
        provider_id=value["provider_id"],
        media_type=value["media_type"],
        payload=value["payload"],
    )


def _read_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _check_receipt_file(fd: int) -> os.stat_result:
    info = os.fstat(fd)
    owner = _owner_id()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or (owner >= 0 and info.st_uid != owner)
        or info.st_size > _MAX_RECEIPT_FILE_BYTES
    ):
        raise OSError("provider receipt file is not a private regular file")
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise
    return info


def _read_receipt_bytes(path: Path) -> bytes:
    fd = os.open(str(path), _read_flags())
    try:
        info = _check_receipt_file(fd)
        chunks = bytearray()
        while len(chunks) <= _MAX_RECEIPT_FILE_BYTES:
            chunk = os.read(
                fd,
                min(65_536, _MAX_RECEIPT_FILE_BYTES + 1 - len(chunks)),
            )
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) > _MAX_RECEIPT_FILE_BYTES or len(chunks) != info.st_size:
            raise OSError("provider receipt file changed while reading")
        return bytes(chunks)
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_receipt(path: Path, encoded: bytes) -> None:
    try:
        existing = _read_receipt_bytes(path)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if existing != encoded:
            raise OSError("content-addressed provider receipt does not match")
        return

    fd, temporary = tempfile.mkstemp(
        prefix=".receipt.", suffix=".tmp", dir=str(path.parent),
    )
    safe_temporary = False
    try:
        temporary_path = Path(temporary)
        path_info = temporary_path.lstat()
        fd_info = os.fstat(fd)
        if (
            temporary_path.parent.absolute() != path.parent.absolute()
            or not stat.S_ISREG(path_info.st_mode)
            or not os.path.samestat(path_info, fd_info)
            or fd_info.st_nlink != 1
        ):
            raise OSError("temporary provider receipt is unsafe")
        safe_temporary = True
        _check_receipt_file(fd)
        offset = 0
        while offset < len(encoded):
            written = os.write(fd, encoded[offset:])
            if written <= 0:
                raise OSError("provider receipt write did not make progress")
            offset += written
        os.fsync(fd)
        try:
            # Hard-link publication is atomic and refuses an existing name;
            # unlike os.replace(), it can never overwrite a path injected
            # after the initial existence check.
            os.link(temporary, str(path))
        except FileExistsError:
            existing = _read_receipt_bytes(path)
            if existing != encoded:
                raise OSError("content-addressed provider receipt does not match")
            return
        os.unlink(temporary)
        safe_temporary = False
        final = path.lstat()
        if (
            not stat.S_ISREG(final.st_mode)
            or final.st_nlink != 1
            or not os.path.samestat(final, os.fstat(fd))
        ):
            raise OSError("provider receipt target is unsafe")
        try:
            _fsync_directory(path.parent)
        except OSError:
            pass
    finally:
        if fd >= 0:
            os.close(fd)
        if safe_temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _save_extension_launch_unlocked(
    provider_id: str,
    receipt: ProviderReceiptEnvelopeV1,
    *,
    provider_home: Optional[str],
) -> StoredExtensionLaunchV1:
    """Persist verified receipt data first, then atomically publish its pointer."""

    provider_id = _require_provider_id(provider_id)
    if type(receipt) is not ProviderReceiptEnvelopeV1 or receipt.provider_id != provider_id:
        raise ValueError("provider receipt does not match provider id")
    provider_home = _checked_path(provider_home, "provider_home")
    encoded = _encoded_envelope(receipt)
    digest = hashlib.sha256(encoded).hexdigest()
    path = _receipt_path(provider_id, digest, create=True)
    _write_receipt(path, encoded)
    pointer = settings.ExtensionLaunchSettingsV1(
        provider_id=provider_id,
        receipt_sha256=digest,
        provider_home=provider_home,
    )
    # Receipt-first ordering means an interrupted settings update can leave at
    # most an inert orphan; settings can never reference an unwritten receipt.
    settings.set_extension_launch_settings(pointer)
    return StoredExtensionLaunchV1(
        provider_id=provider_id,
        receipt_sha256=digest,
        provider_home=provider_home,
        receipt=ProviderReceiptEnvelopeV1(
            provider_id=receipt.provider_id,
            media_type=receipt.media_type,
            payload=receipt.payload,
        ),
    )


def save_extension_launch(
    provider_id: str,
    receipt: ProviderReceiptEnvelopeV1,
    *,
    provider_home: Optional[str],
) -> StoredExtensionLaunchV1:
    """Persist verified receipt data first, then atomically publish its pointer."""

    with _store_lock():
        return _save_extension_launch_unlocked(
            provider_id, receipt, provider_home=provider_home,
        )


def _load_extension_launch_unlocked(
    provider_id: str,
) -> Optional[StoredExtensionLaunchV1]:
    """Load, hash-check, decode, and Core-revalidate one stored receipt."""

    provider_id = _require_provider_id(provider_id)
    pointer = settings.get_extension_launch_settings(provider_id)
    if pointer is None:
        return None
    path = _receipt_path(provider_id, pointer.receipt_sha256, create=False)
    encoded = _read_receipt_bytes(path)
    if hashlib.sha256(encoded).hexdigest() != pointer.receipt_sha256:
        raise ValueError("stored provider receipt digest does not match")
    receipt = _decoded_envelope(encoded)
    if receipt.provider_id != provider_id:
        raise ValueError("stored provider receipt id does not match")
    if _encoded_envelope(receipt) != encoded:
        raise ValueError("stored provider receipt is not canonical")
    return StoredExtensionLaunchV1(
        provider_id=provider_id,
        receipt_sha256=pointer.receipt_sha256,
        provider_home=pointer.provider_home,
        receipt=receipt,
    )


def load_extension_launch(provider_id: str) -> Optional[StoredExtensionLaunchV1]:
    """Load, hash-check, decode, and Core-revalidate one stored receipt."""

    with _store_lock():
        return _load_extension_launch_unlocked(provider_id)


def _clear_extension_launch_unlocked(provider_id: str) -> bool:
    """Unpublish one receipt pointer while preserving its immutable blob."""

    provider_id = _require_provider_id(provider_id)
    try:
        pointer = settings.get_extension_launch_settings(provider_id)
    except ValueError:
        # A malformed reserved pointer must fail closed during load, while the
        # dedicated clear operation remains a safe recovery path.
        return settings.clear_extension_launch_settings(provider_id)
    if pointer is None:
        return False
    removed = settings.clear_extension_launch_settings(provider_id)
    if not removed:
        return False
    # POSIX unlink is pathname-based: a same-user replacement could be
    # substituted after validation and before deletion. Content-addressed
    # blobs are inert once their pointer is removed, so preserving the blob is
    # the only portable identity-safe clear operation.
    return True


def clear_extension_launch(provider_id: str) -> bool:
    """Unpublish one receipt pointer without pathname-based blob deletion."""

    with _store_lock():
        return _clear_extension_launch_unlocked(provider_id)


__all__ = [
    "ExtensionLaunchOverridesV1",
    "StoredExtensionLaunchV1",
    "clear_extension_launch",
    "default_provider_home",
    "load_extension_launch",
    "save_extension_launch",
]
