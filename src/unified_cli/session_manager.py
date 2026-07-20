"""Bounded, provider-namespaced session index for interactive CLI use.

``state.json`` remains the backwards-compatible single last-session record.
This module owns a separate, explicitly versioned ``sessions.json`` index so
new session-management features cannot regress legacy ``--continue`` calls.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import stat
import tempfile
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

try:  # POSIX is the supported platform family.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]


SESSIONS_DIR = Path.home() / ".unified-cli"
SESSIONS_FILE = SESSIONS_DIR / "sessions.json"
_VERSION = 1
_MAX_FILE_BYTES = 1_048_576
_DEFAULT_MAX_RECORDS = 100
_HARD_MAX_RECORDS = 1_000
_MAX_JSON_DEPTH = 6
_MAX_JSON_ITEMS = 128
_MAX_METADATA_BYTES = 32_768
_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_MAX_SESSION_ID_CHARS = 512
_METADATA_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[_.-])(api[_-]?key|auth|bearer|cookie|credential|password|secret|token)(?:$|[_.-])",
    re.IGNORECASE,
)
_LOCK = threading.RLock()


def _sensitive_key(value: str) -> bool:
    flattened = re.sub(r"[^a-z0-9]", "", value.lower())
    return _SENSITIVE_KEY_RE.search(value) is not None or any(
        marker in flattened
        for marker in (
            "apikey", "accesstoken", "authtoken", "authorization", "bearer",
            "cookie", "credential", "password", "passwd", "secret",
        )
    )


@dataclass
class SessionRecord:
    provider: str
    session_id: str
    model: str = ""
    name: str = ""
    cwd: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    archived: bool = False
    forked_from: Optional[Dict[str, str]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.updated_at) if self.updated_at else 0.0


def _provider_id(value: object) -> str:
    if type(value) is not str or _PROVIDER_ID_RE.fullmatch(value) is None:
        raise ValueError("provider must be a safe provider id")
    return value


def _session_id(value: object) -> str:
    if type(value) is not str or not value or len(value) > _MAX_SESSION_ID_CHARS:
        raise ValueError("session_id must be a bounded opaque string")
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise ValueError("session_id must be valid UTF-8 text") from None
    if any(
        unicodedata.category(ch).startswith("C")
        or unicodedata.category(ch) in {"Zl", "Zp"}
        for ch in value
    ):
        raise ValueError("session_id must not contain control characters")
    return value


def _text(value: object, *, field_name: str, maximum: int, empty: bool = True) -> str:
    if (
        type(value) is not str
        or len(value) > maximum
        or (not empty and not value.strip())
        or "\x00" in value
        or any(unicodedata.category(ch).startswith("C") for ch in value)
    ):
        raise ValueError(f"{field_name} must be a safe bounded string")
    return value


def _cwd(value: object) -> str:
    text = _text(value, field_name="cwd", maximum=4096)
    if not text:
        return ""
    path = Path(text).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("cwd must be an absolute normalized path")
    return str(path)


def _timestamp(value: object, field_name: str) -> float:
    if type(value) not in (int, float) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")
    return result


def _json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("metadata is nested too deeply")
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("metadata numbers must be finite")
        return value
    if type(value) is str:
        if len(value) > 4096 or any(
            unicodedata.category(ch).startswith("C") for ch in value
        ):
            raise ValueError("metadata string is invalid or too large")
        return value
    if type(value) is list:
        if len(value) > _MAX_JSON_ITEMS:
            raise ValueError("metadata list is too large")
        return [_json_value(item, depth=depth + 1) for item in value]
    if type(value) is dict:
        if len(value) > _MAX_JSON_ITEMS:
            raise ValueError("metadata object is too large")
        result: Dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str or _METADATA_KEY_RE.fullmatch(key) is None:
                raise ValueError("metadata keys must be safe identifiers")
            if _sensitive_key(key):
                raise ValueError("provider credentials may not be stored in sessions")
            result[key] = _json_value(item, depth=depth + 1)
        return result
    raise ValueError("metadata must contain JSON-safe values only")


def _metadata(value: object) -> Dict[str, Any]:
    if type(value) is not dict:
        raise ValueError("metadata must be an object")
    result = _json_value(value)
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_METADATA_BYTES:
        raise ValueError("metadata is too large")
    return result


def _fork_ref(value: object) -> Optional[Dict[str, str]]:
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"provider", "session_id"}:
        raise ValueError("forked_from must contain provider and session_id")
    return {
        "provider": _provider_id(value["provider"]),
        "session_id": _session_id(value["session_id"]),
    }


def _record(value: Mapping[str, Any], namespace: Optional[str] = None) -> SessionRecord:
    provider = _provider_id(value.get("provider"))
    if namespace is not None and provider != namespace:
        raise ValueError("record crossed provider namespace")
    created_at = _timestamp(value.get("created_at", 0.0), "created_at")
    updated_at = _timestamp(value.get("updated_at", 0.0), "updated_at")
    archived = value.get("archived", False)
    if type(archived) is not bool:
        raise ValueError("archived must be a boolean")
    return SessionRecord(
        provider=provider,
        session_id=_session_id(value.get("session_id")),
        model=_text(value.get("model", ""), field_name="model", maximum=512),
        name=_text(value.get("name", ""), field_name="name", maximum=200),
        cwd=_cwd(value.get("cwd", "")),
        created_at=created_at,
        updated_at=updated_at,
        archived=archived,
        forked_from=_fork_ref(value.get("forked_from")),
        metadata=_metadata(value.get("metadata", {})),
    )


def _copy(record: SessionRecord) -> SessionRecord:
    return copy.deepcopy(record)


class SessionManager:
    """Durable bounded index, keyed by ``(provider, session_id)``."""

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        max_records: int = _DEFAULT_MAX_RECORDS,
    ) -> None:
        if type(max_records) is not int or not 1 <= max_records <= _HARD_MAX_RECORDS:
            raise ValueError(f"max_records must be from 1 to {_HARD_MAX_RECORDS}")
        candidate = Path(path) if path is not None else SESSIONS_FILE
        if not candidate.is_absolute():
            raise ValueError("sessions path must be absolute")
        self.path = candidate
        self.directory = self.path.parent
        self.max_records = max_records
        self._thread_lock = threading.RLock()

    def _ensure_dir(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OSError("sessions directory must be a real directory")
        try:
            os.chmod(str(self.directory), 0o700, follow_symlinks=False)
        except OSError:
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        with _LOCK, self._thread_lock:
            self._ensure_dir()
            lock_path = self.directory / "sessions.lock"
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(str(lock_path), flags, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise OSError("sessions lock must be a regular file")
                os.fchmod(fd, 0o600)
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _read(self) -> Dict[Tuple[str, str], SessionRecord]:
        try:
            if not self.directory.exists():
                return {}
            self._ensure_dir()
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(str(self.path), flags)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE_BYTES:
                    return {}
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    if stat.S_IMODE(info.st_mode) & 0o077:
                        return {}
                body = bytearray()
                while len(body) <= _MAX_FILE_BYTES:
                    part = os.read(fd, min(65_536, _MAX_FILE_BYTES + 1 - len(body)))
                    if not part:
                        break
                    body.extend(part)
                if len(body) > _MAX_FILE_BYTES:
                    return {}
            finally:
                os.close(fd)
            data = json.loads(bytes(body).decode("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeError, ValueError):
            return {}

        if (
            type(data) is not dict
            or type(data.get("version")) is not int
            or data.get("version") != _VERSION
        ):
            return {}
        providers = data.get("providers")
        if type(providers) is not dict or len(providers) > _HARD_MAX_RECORDS:
            return {}

        result: Dict[Tuple[str, str], SessionRecord] = {}
        examined = 0
        for namespace in sorted(providers):
            try:
                provider = _provider_id(namespace)
            except ValueError:
                continue
            values = providers[namespace]
            if type(values) is not list:
                continue
            for value in values:
                examined += 1
                if examined > _HARD_MAX_RECORDS:
                    return {}
                try:
                    if type(value) is not dict:
                        raise ValueError("session record must be an object")
                    record = _record(value, provider)
                except (TypeError, ValueError):
                    continue
                key = (record.provider, record.session_id)
                if key not in result:  # deterministic first-wins on untrusted duplicates
                    result[key] = record
        self._prune(result)
        return result

    def _check_temp_target(self, fd: int, tmp_path: str) -> None:
        path = Path(tmp_path)
        if path.parent.absolute() != self.directory.absolute():
            raise OSError("temporary file must be created in the sessions directory")
        path_info = path.lstat()
        if not stat.S_ISREG(path_info.st_mode) or not os.path.samestat(path_info, os.fstat(fd)):
            raise OSError("temporary sessions target is not a regular owned file")

    def _fsync_dir(self) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(self.directory), flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _write(self, records: Mapping[Tuple[str, str], SessionRecord]) -> None:
        providers: Dict[str, List[dict]] = {}
        for record in sorted(
            records.values(), key=lambda item: (item.provider, -item.updated_at, item.session_id),
        ):
            providers.setdefault(record.provider, []).append(asdict(record))
        payload = {"version": _VERSION, "providers": providers}
        encoded = json.dumps(
            payload, ensure_ascii=False, indent=2, allow_nan=False,
        )
        if len(encoded.encode("utf-8")) > _MAX_FILE_BYTES:
            raise ValueError("sessions index is too large")
        fd, tmp_path = tempfile.mkstemp(
            prefix=".sessions.", suffix=".json", dir=str(self.directory),
        )
        temp_is_safe = False
        try:
            self._check_temp_target(fd, tmp_path)
            temp_is_safe = True
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, str(self.path))
            os.chmod(str(self.path), 0o600, follow_symlinks=False)
            try:
                self._fsync_dir()
            except OSError:
                pass
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            if temp_is_safe:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise

    def _prune(self, records: Dict[Tuple[str, str], SessionRecord]) -> None:
        if len(records) <= self.max_records:
            return
        oldest_first = sorted(
            records.items(),
            key=lambda item: (
                not item[1].archived,
                item[1].updated_at,
                item[1].created_at,
                item[0],
            ),
        )
        for key, _record_value in oldest_first[:len(records) - self.max_records]:
            del records[key]

    def list(
        self,
        *,
        provider: Optional[str] = None,
        include_archived: bool = False,
    ) -> List[SessionRecord]:
        """Return detached records, newest first, with deterministic ties."""
        if provider is not None:
            provider = _provider_id(provider)
        if type(include_archived) is not bool:
            raise ValueError("include_archived must be a boolean")
        records = self._read().values()
        selected = [
            record for record in records
            if (provider is None or record.provider == provider)
            and (include_archived or not record.archived)
        ]
        selected.sort(key=lambda item: (-item.updated_at, item.provider, item.session_id))
        return [_copy(record) for record in selected]

    def get(self, *, provider: str, session_id: str) -> Optional[SessionRecord]:
        key = (_provider_id(provider), _session_id(session_id))
        record = self._read().get(key)
        return _copy(record) if record is not None else None

    def upsert(
        self,
        *,
        provider: str,
        session_id: str,
        model: str = "",
        name: Optional[str] = None,
        cwd: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        archived: Optional[bool] = None,
        updated_at: Optional[float] = None,
    ) -> SessionRecord:
        provider = _provider_id(provider)
        session_id = _session_id(session_id)
        model = _text(model, field_name="model", maximum=512)
        if name is not None:
            name = _text(name, field_name="name", maximum=200)
        if cwd is not None:
            cwd = _cwd(cwd)
        if metadata is not None:
            metadata = _metadata(metadata)
        if archived is not None and type(archived) is not bool:
            raise ValueError("archived must be a boolean or None")
        now = time.time() if updated_at is None else _timestamp(updated_at, "updated_at")
        key = (provider, session_id)
        with self._file_lock():
            records = self._read()
            existing = records.get(key)
            if existing is None:
                record = SessionRecord(
                    provider=provider,
                    session_id=session_id,
                    model=model,
                    name=name or "",
                    cwd=_cwd(cwd if cwd is not None else os.getcwd()),
                    created_at=now,
                    updated_at=now,
                    archived=archived if archived is not None else False,
                    metadata=metadata or {},
                )
            else:
                record = _copy(existing)
                record.model = model or record.model
                if name is not None:
                    record.name = name
                if cwd is not None:
                    record.cwd = cwd
                if metadata is not None:
                    record.metadata = metadata
                if archived is not None:
                    record.archived = archived
                record.updated_at = now
            records[key] = _record(asdict(record), provider)
            self._prune(records)
            self._write(records)
            return _copy(record)

    def rename(self, *, provider: str, session_id: str, name: str) -> SessionRecord:
        key = (_provider_id(provider), _session_id(session_id))
        name = _text(name, field_name="name", maximum=200, empty=False)
        with self._file_lock():
            records = self._read()
            if key not in records:
                raise KeyError(session_id)
            record = _copy(records[key])
            record.name = name.strip()
            record.updated_at = time.time()
            records[key] = record
            self._write(records)
            return _copy(record)

    def fork(
        self,
        *,
        source_provider: str,
        source_session_id: str,
        provider: str,
        session_id: str,
        model: Optional[str] = None,
        name: Optional[str] = None,
        cwd: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SessionRecord:
        source_key = (_provider_id(source_provider), _session_id(source_session_id))
        provider = _provider_id(provider)
        session_id = _session_id(session_id)
        target_key = (provider, session_id)
        if model is not None:
            model = _text(model, field_name="model", maximum=512)
        if name is not None:
            name = _text(name, field_name="name", maximum=200)
        if cwd is not None:
            cwd = _cwd(cwd)
        if metadata is not None:
            metadata = _metadata(metadata)

        with self._file_lock():
            records = self._read()
            source = records.get(source_key)
            if source is None:
                raise KeyError(source_session_id)
            if target_key in records:
                raise ValueError("fork target already exists")
            now = time.time()
            record = SessionRecord(
                provider=provider,
                session_id=session_id,
                model=source.model if model is None else model,
                name=source.name if name is None else name,
                cwd=source.cwd if cwd is None else cwd,
                created_at=now,
                updated_at=now,
                archived=False,
                forked_from={"provider": source.provider, "session_id": source.session_id},
                metadata=_copy(source).metadata if metadata is None else metadata,
            )
            records[target_key] = _record(asdict(record), provider)
            self._prune(records)
            self._write(records)
            return _copy(record)

    def record_fork(
        self,
        *,
        source_provider: str,
        source_session_id: str,
        provider: str,
        session_id: str,
        model: Optional[str] = None,
        name: Optional[str] = None,
        cwd: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SessionRecord:
        """Explicitly named alias for :meth:`fork`."""
        return self.fork(
            source_provider=source_provider,
            source_session_id=source_session_id,
            provider=provider,
            session_id=session_id,
            model=model,
            name=name,
            cwd=cwd,
            metadata=metadata,
        )

    def archive(
        self,
        *,
        provider: str,
        session_id: str,
        archived: bool = True,
    ) -> SessionRecord:
        key = (_provider_id(provider), _session_id(session_id))
        if type(archived) is not bool:
            raise ValueError("archived must be a boolean")
        with self._file_lock():
            records = self._read()
            if key not in records:
                raise KeyError(session_id)
            record = _copy(records[key])
            record.archived = archived
            record.updated_at = time.time()
            records[key] = record
            self._write(records)
            return _copy(record)

    def delete(self, *, provider: str, session_id: str) -> bool:
        key = (_provider_id(provider), _session_id(session_id))
        with self._file_lock():
            records = self._read()
            if key not in records:
                return False
            del records[key]
            self._write(records)
            return True

    def clear(self, *, provider: Optional[str] = None) -> int:
        if provider is not None:
            provider = _provider_id(provider)
        with self._file_lock():
            records = self._read()
            if provider is None:
                removed = len(records)
                records.clear()
            else:
                keys = [key for key in records if key[0] == provider]
                removed = len(keys)
                for key in keys:
                    del records[key]
            if removed:
                self._write(records)
            return removed


__all__ = ["SessionManager", "SessionRecord", "SESSIONS_DIR", "SESSIONS_FILE"]
