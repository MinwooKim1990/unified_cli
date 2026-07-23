"""CLI-level last-session persistence.

This module retains the original v1 wire format and public API.  The richer,
multi-session index lives in :mod:`unified_cli.session_manager`; keeping the
files separate makes the legacy ``--continue`` behavior independently robust.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from .core import ProviderId


STATE_DIR = Path.home() / ".unified-cli"
STATE_FILE = STATE_DIR / "state.json"
_VERSION = 1
_MAX_FILE_BYTES = 262_144
_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_MAX_SESSION_ID_CHARS = 512
_LOCK = threading.RLock()


@dataclass
class SessionState:
    provider: ProviderId
    model: str
    session_id: str
    cwd: str = ""
    updated_at: float = 0.0

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.updated_at) if self.updated_at else 0.0


def resolve_cwd(cwd: Optional[str]) -> Optional[str]:
    """Return one existing canonical directory, or ``None`` when invalid."""
    if not isinstance(cwd, str) or not cwd.strip() or len(cwd) > 4096 or "\x00" in cwd:
        return None
    try:
        resolved = Path(cwd).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return str(resolved) if resolved.is_dir() else None


def _safe_text(value: object, maximum: int, *, allow_empty: bool = False) -> bool:
    return (
        type(value) is str
        and (allow_empty or bool(value))
        and len(value) <= maximum
        and "\x00" not in value
        and not any(unicodedata.category(ch).startswith("C") for ch in value)
    )


def _safe_session_id(value: object) -> bool:
    """Accept a bounded opaque provider reference, never interpret it as a path."""
    if type(value) is not str or not value or len(value) > _MAX_SESSION_ID_CHARS:
        return False
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return False
    return not any(
        unicodedata.category(ch).startswith("C")
        or unicodedata.category(ch) in {"Zl", "Zp"}
        for ch in value
    )


def _validated_state(
    provider: object,
    model: object,
    session_id: object,
    cwd: object,
    updated_at: object,
) -> Optional[SessionState]:
    if type(provider) is not str or _PROVIDER_ID_RE.fullmatch(provider) is None:
        return None
    if not _safe_text(model, 512) or not _safe_session_id(session_id):
        return None
    if not _safe_text(cwd, 4096, allow_empty=True):
        return None
    if type(updated_at) not in (int, float) or isinstance(updated_at, bool):
        return None
    timestamp = float(updated_at)
    if not math.isfinite(timestamp) or timestamp < 0:
        return None
    return SessionState(provider, model, session_id, cwd, timestamp)


def _ensure_dir() -> None:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = STATE_DIR.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OSError("state directory must be a real directory")
    os.chmod(str(STATE_DIR), 0o700, follow_symlinks=False)


def _read_payload() -> Optional[dict]:
    try:
        if not STATE_DIR.exists():
            return None
        _ensure_dir()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(STATE_FILE), flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE_BYTES:
                return None
            os.fchmod(fd, 0o600)
            body = bytearray()
            while len(body) <= _MAX_FILE_BYTES:
                part = os.read(fd, min(65_536, _MAX_FILE_BYTES + 1 - len(body)))
                if not part:
                    break
                body.extend(part)
            if len(body) > _MAX_FILE_BYTES:
                return None
        finally:
            os.close(fd)
        value = json.loads(bytes(body).decode("utf-8"))
        return value if type(value) is dict else None
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeError, ValueError):
        return None


def _check_temp_target(fd: int, tmp_path: str) -> None:
    """Reject a monkeypatched/unsafe temporary target before writing to it."""
    path = Path(tmp_path)
    if path.parent.absolute() != STATE_DIR.absolute():
        raise OSError("temporary file must be created in the state directory")
    path_info = path.lstat()
    fd_info = os.fstat(fd)
    if not stat.S_ISREG(path_info.st_mode) or not os.path.samestat(path_info, fd_info):
        raise OSError("temporary state target is not a regular owned file")


def _fsync_dir() -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(STATE_DIR), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save_last_session(
    provider: ProviderId,
    model: str,
    session_id: str,
    cwd: Optional[str] = None,
) -> None:
    """Atomically overwrite the v1 last-session record."""
    effective_cwd = cwd if cwd is not None else os.getcwd()
    state = _validated_state(provider, model, session_id, effective_cwd, time.time())
    if state is None:
        raise ValueError("invalid last-session state")
    payload = {"version": _VERSION, "last_session": asdict(state)}

    with _LOCK:
        _ensure_dir()
        fd, tmp_path = tempfile.mkstemp(
            prefix=".state.", suffix=".json", dir=str(STATE_DIR),
        )
        temp_is_safe = False
        try:
            _check_temp_target(fd, tmp_path)
            temp_is_safe = True
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, str(STATE_FILE))
            os.chmod(str(STATE_FILE), 0o600, follow_symlinks=False)
            try:
                _fsync_dir()
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


def load_last_session() -> Optional[SessionState]:
    """Return the saved v1 last-session record, or ``None`` if untrusted."""
    data = _read_payload()
    if (
        data is None
        or type(data.get("version")) is not int
        or data.get("version") != _VERSION
    ):
        return None
    inner = data.get("last_session")
    if type(inner) is not dict:
        return None
    return _validated_state(
        inner.get("provider"),
        inner.get("model"),
        inner.get("session_id"),
        inner.get("cwd", ""),
        inner.get("updated_at", 0.0),
    )


def clear_last_session() -> bool:
    """Remove the state file without following it; return whether it existed."""
    with _LOCK:
        try:
            if STATE_DIR.exists():
                info = STATE_DIR.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    return False
            STATE_FILE.unlink()
            try:
                _fsync_dir()
            except OSError:
                pass
            return True
        except (FileNotFoundError, OSError):
            return False


__all__ = [
    "SessionState", "STATE_DIR", "STATE_FILE", "resolve_cwd",
    "save_last_session", "load_last_session", "clear_last_session",
]
