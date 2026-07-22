"""Versioned, security-conscious user settings for the CLI and REPL.

The settings file is deliberately self-contained.  In particular, loading it
must never discover or import provider plugins: extension settings are merely
validated JSON values stored below a provider-id namespace.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional

try:  # unified-cli supports POSIX; keep import-time compatibility elsewhere.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]


SETTINGS_DIR = Path.home() / ".unified-cli"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"
_VERSION = 2
_LEGACY_VERSION = 1
_MAX_FILE_BYTES = 1_048_576
_MAX_PROVIDER_NAMESPACES = 64
_MAX_JSON_DEPTH = 6
_MAX_JSON_ITEMS = 256
_MAX_NAMESPACE_BYTES = 65_536
_PROVIDERS = frozenset(("claude", "codex", "gemini"))
_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SETTING_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_RECEIPT_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LANG_RE = re.compile(r"^[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8}){0,2}$")
_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[_.-])(api[_-]?key|auth|bearer|cookie|credential|password|secret|token)(?:$|[_.-])",
    re.IGNORECASE,
)
_LOCK = threading.RLock()
_EXTENSION_LAUNCH_KEY = "unified_cli_launch"
_EXTENSION_LAUNCH_SCHEMA_V1 = 1
_MISSING = object()


@dataclass(frozen=True)
class ExtensionLaunchSettingsV1:
    """Small settings pointer to a separately stored provider receipt."""

    provider_id: str
    receipt_sha256: str
    provider_home: Optional[str] = None

    def __post_init__(self) -> None:
        if not _safe_provider_id(self.provider_id):
            raise ValueError("invalid provider id")
        if (
            type(self.receipt_sha256) is not str
            or _RECEIPT_SHA256_RE.fullmatch(self.receipt_sha256) is None
        ):
            raise ValueError("invalid provider receipt digest")
        if self.provider_home is not None:
            if type(self.provider_home) is not str:
                raise ValueError("invalid provider home")
            try:
                encoded_home = os.fsencode(self.provider_home)
            except (TypeError, UnicodeError):
                raise ValueError("invalid provider home") from None
            if (
                not self.provider_home
                or "\x00" in self.provider_home
                or len(encoded_home) > 16 * 1024
                or not os.path.isabs(self.provider_home)
                or os.path.normpath(self.provider_home) != self.provider_home
            ):
                raise ValueError("invalid provider home")


@dataclass
class Settings:
    # The first two fields intentionally retain the v1 constructor/API.
    lang: Optional[str] = None
    default_provider: Optional[str] = None  # None means the Core Claude default.

    reasoning_display: str = "hidden"
    tool_display: str = "compact"
    theme: str = "auto"
    cross_provider_context_enabled: bool = True
    context_window: int = 8
    repl_permission: str = "provider_default"
    browser_permission: str = "read_only"
    browser_prompt_preview: bool = False

    # Durable REPL preferences. None delegates to provider/default behavior.
    style: Optional[str] = None
    effort: Optional[str] = None
    reasoning_mode: Optional[str] = None
    system_prompt: Optional[str] = None
    timeout: Optional[float] = None
    tools: Optional[bool] = None
    mcp: Optional[bool] = None
    web: Optional[bool] = None
    workspace: Optional[str] = None
    additional_dirs: List[str] = field(default_factory=list)
    multiline: bool = True

    # Provider data is always nested under a validated provider identifier.
    provider_settings: Dict[str, Dict[str, Any]] = field(default_factory=dict)


def _safe_provider_id(value: object) -> bool:
    return type(value) is str and _PROVIDER_ID_RE.fullmatch(value) is not None


def _is_sensitive_key(value: str) -> bool:
    flattened = re.sub(r"[^a-z0-9]", "", value.lower())
    return _SENSITIVE_KEY_RE.search(value) is not None or any(
        marker in flattened
        for marker in (
            "apikey", "accesstoken", "authtoken", "authorization", "bearer",
            "cookie", "credential", "password", "passwd", "secret",
        )
    )


def _safe_json(value: Any, *, depth: int = 0) -> Any:
    """Return an independent, bounded JSON-safe value or raise ValueError."""
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("provider setting is nested too deeply")
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("provider setting numbers must be finite")
        return value
    if type(value) is str:
        if len(value) > 4096 or any(ord(ch) < 32 and ch not in "\t\n\r" for ch in value):
            raise ValueError("provider setting string is invalid or too long")
        return value
    if type(value) is list:
        if len(value) > _MAX_JSON_ITEMS:
            raise ValueError("provider setting list is too large")
        return [_safe_json(item, depth=depth + 1) for item in value]
    if type(value) is dict:
        if len(value) > _MAX_JSON_ITEMS:
            raise ValueError("provider setting object is too large")
        result: Dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str or _SETTING_KEY_RE.fullmatch(key) is None:
                raise ValueError("provider setting keys must be safe identifiers")
            if _is_sensitive_key(key):
                raise ValueError("provider credentials may not be persisted")
            result[key] = _safe_json(item, depth=depth + 1)
        return result
    raise ValueError("provider settings must contain JSON-safe values only")


def _extension_launch_record(
    value: object, *, provider_id: str
) -> ExtensionLaunchSettingsV1:
    if type(value) is not dict or frozenset(value) != frozenset({
        "schema", "receipt_sha256", "provider_home",
    }):
        raise ValueError("invalid extension launch settings")
    if (
        type(value["schema"]) is not int
        or value["schema"] != _EXTENSION_LAUNCH_SCHEMA_V1
    ):
        raise ValueError("unsupported extension launch settings")
    return ExtensionLaunchSettingsV1(
        provider_id=provider_id,
        receipt_sha256=value["receipt_sha256"],
        provider_home=value["provider_home"],
    )


def _extension_launch_json(value: ExtensionLaunchSettingsV1) -> Dict[str, Any]:
    if type(value) is not ExtensionLaunchSettingsV1:
        raise TypeError("launch settings must be ExtensionLaunchSettingsV1")
    # Reconstruct through the public type before crossing the persistence
    # boundary; do not trust a mutated/object.__setattr__-forged instance.
    checked = ExtensionLaunchSettingsV1(
        provider_id=value.provider_id,
        receipt_sha256=value.receipt_sha256,
        provider_home=value.provider_home,
    )
    return {
        "schema": _EXTENSION_LAUNCH_SCHEMA_V1,
        "receipt_sha256": checked.receipt_sha256,
        "provider_home": checked.provider_home,
    }


def _provider_settings(value: object, *, strict: bool) -> Dict[str, Dict[str, Any]]:
    if type(value) is not dict or len(value) > _MAX_PROVIDER_NAMESPACES:
        if strict:
            raise ValueError("provider_settings must be a bounded object")
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for provider_id, namespace in value.items():
        try:
            if not _safe_provider_id(provider_id) or type(namespace) is not dict:
                raise ValueError("invalid provider settings namespace")
            ordinary = dict(namespace)
            launch_value = ordinary.pop(_EXTENSION_LAUNCH_KEY, _MISSING)
            safe = _safe_json(ordinary)
            if launch_value is not _MISSING:
                try:
                    launch = _extension_launch_record(
                        launch_value, provider_id=provider_id,
                    )
                    safe[_EXTENSION_LAUNCH_KEY] = _extension_launch_json(launch)
                except (TypeError, ValueError):
                    if strict:
                        raise
                    # Corrupt internal launch metadata is discarded without
                    # poisoning unrelated settings in this provider namespace.
            encoded = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > _MAX_NAMESPACE_BYTES:
                raise ValueError("provider settings namespace is too large")
            result[provider_id] = safe
        except (TypeError, ValueError, UnicodeError):
            if strict:
                raise ValueError("invalid provider settings namespace") from None
            # A bad extension namespace cannot poison another provider's data.
            continue
    return result


def _optional_text(
    value: object,
    *,
    maximum: int,
    strict: bool,
    multiline: bool = False,
) -> Optional[str]:
    if value is None:
        return None
    if type(value) is str and len(value) <= maximum:
        allowed_controls = frozenset(("\n", "\r", "\t")) if multiline else frozenset()
        if not any(
            (
                unicodedata.category(char).startswith("C")
                and char not in allowed_controls
            )
            or unicodedata.category(char) in {"Zl", "Zp"}
            for char in value
        ):
            return value
    if strict:
        raise ValueError("setting must be a bounded string or None")
    return None


def _path_text(value: object, *, strict: bool) -> Optional[str]:
    value = _optional_text(value, maximum=4096, strict=strict)
    if value is None:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        if strict:
            raise ValueError("workspace paths must be absolute and normalized")
        return None
    return str(path)


def _choice(value: object, allowed: set, default: str, *, strict: bool) -> str:
    if type(value) is str and value in allowed:
        return value
    if strict:
        raise ValueError("unsupported setting value")
    return default


def _normalise(raw: Mapping[str, Any], *, strict: bool) -> Settings:
    """Build a typed Settings object, rejecting on save and failing closed on load."""
    lang = raw.get("lang")
    if lang is not None and (type(lang) is not str or _LANG_RE.fullmatch(lang) is None):
        if strict:
            raise ValueError("lang must be a short locale identifier or None")
        lang = None

    default_provider = raw.get("default_provider")
    if default_provider is not None and (
        type(default_provider) is not str or default_provider not in _PROVIDERS
    ):
        if strict:
            raise ValueError(
                "default_provider must be one of: " + ", ".join(sorted(_PROVIDERS))
            )
        default_provider = None

    context_window = raw.get("context_window", 8)
    if type(context_window) is not int or not 1 <= context_window <= 128:
        if strict:
            raise ValueError("context_window must be an integer from 1 to 128")
        context_window = 8

    timeout_value = raw.get("timeout")
    timeout: Optional[float]
    if timeout_value is None:
        timeout = None
    elif type(timeout_value) in (int, float) and not isinstance(timeout_value, bool):
        timeout = float(timeout_value)
        if not math.isfinite(timeout) or not 0.1 <= timeout <= 86_400:
            if strict:
                raise ValueError("timeout must be between 0.1 and 86400 seconds")
            timeout = None
    else:
        if strict:
            raise ValueError("timeout must be a number or None")
        timeout = None

    additional_dirs_value = raw.get("additional_dirs", [])
    additional_dirs: List[str] = []
    if type(additional_dirs_value) is list and len(additional_dirs_value) <= 32:
        for item in additional_dirs_value:
            parsed = _path_text(item, strict=strict)
            if parsed is None:
                if strict:
                    raise ValueError("additional_dirs contains an invalid path")
                continue
            if parsed not in additional_dirs:
                additional_dirs.append(parsed)
    elif strict:
        raise ValueError("additional_dirs must be a list of at most 32 paths")

    bool_defaults = {
        "cross_provider_context_enabled": True,
        "browser_prompt_preview": False,
        "multiline": True,
    }
    bool_values: Dict[str, bool] = {}
    for key, default in bool_defaults.items():
        value = raw.get(key, default)
        if type(value) is not bool:
            if strict:
                raise ValueError(f"{key} must be a boolean")
            value = default
        bool_values[key] = value

    tri_state: Dict[str, Optional[bool]] = {}
    for key in ("tools", "mcp", "web"):
        value = raw.get(key)
        if value is not None and type(value) is not bool:
            if strict:
                raise ValueError(f"{key} must be a boolean or None")
            value = None
        tri_state[key] = value

    return Settings(
        lang=lang,
        default_provider=default_provider,
        reasoning_display=_choice(
            raw.get("reasoning_display", "hidden"),
            {"hidden", "compact", "full"}, "hidden", strict=strict,
        ),
        tool_display=_choice(
            raw.get("tool_display", "compact"),
            {"hidden", "compact", "full"}, "compact", strict=strict,
        ),
        theme=_choice(
            raw.get("theme", "auto"), {"auto", "light", "dark"}, "auto", strict=strict,
        ),
        cross_provider_context_enabled=bool_values["cross_provider_context_enabled"],
        context_window=context_window,
        repl_permission=_choice(
            raw.get("repl_permission", "provider_default"),
            {"provider_default", "read_only", "workspace_write"},
            "provider_default", strict=strict,
        ),
        browser_permission=_choice(
            raw.get("browser_permission", "read_only"),
            {"disabled", "read_only"}, "read_only", strict=strict,
        ),
        browser_prompt_preview=bool_values["browser_prompt_preview"],
        style=_optional_text(raw.get("style"), maximum=128, strict=strict),
        effort=_optional_text(raw.get("effort"), maximum=128, strict=strict),
        reasoning_mode=_optional_text(raw.get("reasoning_mode"), maximum=128, strict=strict),
        system_prompt=_optional_text(
            raw.get("system_prompt"), maximum=32_768, strict=strict, multiline=True,
        ),
        timeout=timeout,
        tools=tri_state["tools"],
        mcp=tri_state["mcp"],
        web=tri_state["web"],
        workspace=_path_text(raw.get("workspace"), strict=strict),
        additional_dirs=additional_dirs,
        multiline=bool_values["multiline"],
        provider_settings=_provider_settings(raw.get("provider_settings", {}), strict=strict),
    )


def _ensure_private_dir() -> None:
    """Create/repair the state directory without accepting a symlink target."""
    try:
        SETTINGS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = SETTINGS_DIR.lstat()
    except OSError:
        raise
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OSError("settings directory must be a real directory")
    try:
        os.chmod(str(SETTINGS_DIR), 0o700, follow_symlinks=False)
    except OSError:
        # A read-only 0500 directory is still private enough to read from.
        # Group/other-accessible directories must fail closed if not repairable.
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise


def _open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _read_payload() -> Optional[dict]:
    try:
        if not SETTINGS_DIR.exists():
            return None
        _ensure_private_dir()
        fd = os.open(str(SETTINGS_FILE), _open_flags())
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE_BYTES:
                return None
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                if stat.S_IMODE(info.st_mode) & 0o077:
                    return None
            chunks = bytearray()
            while len(chunks) <= _MAX_FILE_BYTES:
                chunk = os.read(fd, min(65_536, _MAX_FILE_BYTES + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
            if len(chunks) > _MAX_FILE_BYTES:
                return None
        finally:
            os.close(fd)
        data = json.loads(bytes(chunks).decode("utf-8"))
        return data if type(data) is dict else None
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeError, ValueError):
        return None


@contextmanager
def _file_lock() -> Iterator[None]:
    """Serialize load-modify-save operations across threads and processes."""
    with _LOCK:
        _ensure_private_dir()
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(SETTINGS_DIR / "settings.lock"), flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("settings lock must be a regular file")
            os.fchmod(fd, 0o600)
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _fsync_dir() -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(SETTINGS_DIR), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _check_temp_target(fd: int, tmp_path: str) -> None:
    path = Path(tmp_path)
    if path.parent.absolute() != SETTINGS_DIR.absolute():
        raise OSError("temporary file must be created in the settings directory")
    path_info = path.lstat()
    if not stat.S_ISREG(path_info.st_mode) or not os.path.samestat(path_info, os.fstat(fd)):
        raise OSError("temporary settings target is not a regular owned file")


def _save_unlocked(settings: Settings) -> None:
    payload = {"version": _VERSION, "settings": asdict(settings)}
    encoded = json.dumps(
        payload, ensure_ascii=False, indent=2, allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > _MAX_FILE_BYTES:
        raise ValueError("settings payload is too large")
    fd, tmp_path = tempfile.mkstemp(
        prefix=".settings.", suffix=".json", dir=str(SETTINGS_DIR),
    )
    temp_is_safe = False
    try:
        _check_temp_target(fd, tmp_path)
        temp_is_safe = True
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, str(SETTINGS_FILE))
        os.chmod(str(SETTINGS_FILE), 0o600, follow_symlinks=False)
        try:
            _fsync_dir()
        except OSError:
            # Some otherwise safe filesystems do not support directory fsync.
            pass
    except Exception:
        # ``_check_temp_target`` and ``os.fchmod`` can fail before fdopen()
        # takes ownership.  Closing an already-closed descriptor is harmless
        # here and avoids leaking one on hostile/monkeypatched temp targets.
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


def load_settings() -> Settings:
    """Load v2 settings, transparently migrating a valid v1 document.

    Missing, malformed, oversized and future-version files fail closed without
    exceptions. Individual invalid v2 values are reset to their safe defaults.
    """
    data = _read_payload()
    if data is None:
        return Settings()
    version = data.get("version")
    inner = data.get("settings")
    if (
        type(version) is not int
        or version not in (_LEGACY_VERSION, _VERSION)
        or type(inner) is not dict
    ):
        return Settings()
    if version == _LEGACY_VERSION:
        # Migration is intentionally in-memory on read.  load_settings() is on
        # fast, otherwise read-only paths such as i18n and ``--version``; the
        # next explicit save/set writes the canonical v2 representation.
        return _normalise(
            {"lang": inner.get("lang"), "default_provider": inner.get("default_provider")},
            strict=False,
        )
    return _normalise(inner, strict=False)


def _reserved_launch_values(
    provider_settings: Mapping[str, Mapping[str, Any]],
) -> Dict[str, ExtensionLaunchSettingsV1]:
    result = {}
    for provider_id, namespace in provider_settings.items():
        if _EXTENSION_LAUNCH_KEY in namespace:
            result[provider_id] = _extension_launch_record(
                namespace[_EXTENSION_LAUNCH_KEY], provider_id=provider_id,
            )
    return result


def _raw_reserved_launch_values(
    inner: object,
) -> Dict[str, ExtensionLaunchSettingsV1]:
    """Read reserved records from raw v2 data without lossy normalization."""

    if type(inner) is not dict:
        return {}
    namespaces = inner.get("provider_settings", {})
    if type(namespaces) is not dict:
        return {}
    result = {}
    for provider_id, namespace in namespaces.items():
        if type(namespace) is not dict or _EXTENSION_LAUNCH_KEY not in namespace:
            continue
        try:
            result[provider_id] = _extension_launch_record(
                namespace[_EXTENSION_LAUNCH_KEY], provider_id=provider_id,
            )
        except (TypeError, ValueError):
            # Ordinary writes may not normalize a malformed reserved record
            # away and thereby re-enable unpinned fallback. The typed clear
            # API is the explicit recovery mechanism.
            raise ValueError(
                "reserved extension launch settings are invalid"
            ) from None
    return result


def _write_state_unlocked(
) -> tuple[Settings, Dict[str, ExtensionLaunchSettingsV1]]:
    data = _read_payload()
    version = data.get("version") if data else None
    inner = data.get("settings") if data else None
    launches = {}
    if type(version) is int and version == _VERSION and type(inner) is dict:
        launches = _raw_reserved_launch_values(inner)
    if type(version) is int and version in (_LEGACY_VERSION, _VERSION):
        raw = inner if type(inner) is dict else {}
        if version == _LEGACY_VERSION:
            raw = {
                "lang": raw.get("lang"),
                "default_provider": raw.get("default_provider"),
            }
        current = _normalise(raw, strict=False)
    else:
        current = Settings()
    return current, launches


def _with_current_launches(
    value: Settings,
    launches: Mapping[str, ExtensionLaunchSettingsV1],
) -> Settings:
    candidate = asdict(value)
    namespaces = _provider_settings(
        candidate.get("provider_settings", {}), strict=True,
    )
    # The current raw document is authoritative. Strip any snapshot copies,
    # then merge only records observed while holding the file lock.
    for namespace in namespaces.values():
        namespace.pop(_EXTENSION_LAUNCH_KEY, None)
    for provider_id, launch in launches.items():
        namespace = dict(namespaces.get(provider_id, {}))
        namespace[_EXTENSION_LAUNCH_KEY] = _extension_launch_json(launch)
        namespaces[provider_id] = namespace
    candidate["provider_settings"] = namespaces
    return _normalise(candidate, strict=True)


def save_settings(settings: Settings) -> None:
    """Validate and atomically write settings with private filesystem modes."""
    if not isinstance(settings, Settings):
        raise TypeError("settings must be a Settings instance")
    raw = {
        name: getattr(settings, name)
        for name in Settings.__dataclass_fields__
    }
    normalised = _normalise(raw, strict=True)
    supplied_launch_values = _reserved_launch_values(
        normalised.provider_settings
    )
    with _file_lock():
        _current, current_launch_values = _write_state_unlocked()
        if any(
            current_launch_values.get(provider_id) != launch
            for provider_id, launch in supplied_launch_values.items()
        ):
            raise ValueError(
                "reserved extension launch settings require the typed API"
            )
        _save_unlocked(_with_current_launches(normalised, current_launch_values))


def get(key: str, default: Any = None) -> Any:
    """Read one setting (load-on-read; fine for low-frequency CLI use)."""
    return getattr(load_settings(), key, default)


def set(key: str, value: Any) -> None:  # noqa: A001 - deliberate get/set pair
    """Atomically update one validated top-level setting."""
    if key not in Settings.__dataclass_fields__:
        raise KeyError(key)
    if key == "provider_settings" and type(value) is dict and any(
        type(namespace) is dict and _EXTENSION_LAUNCH_KEY in namespace
        for namespace in value.values()
    ):
        raise ValueError(
            "reserved extension launch settings require the typed settings API"
        )
    with _file_lock():
        settings, launches = _write_state_unlocked()
        candidate = asdict(settings)
        candidate[key] = value
        normalised = _normalise(candidate, strict=True)
        _save_unlocked(_with_current_launches(normalised, launches))


def get_provider_settings(provider_id: str) -> Dict[str, Any]:
    """Return an independent copy of one provider's namespaced settings."""
    if not _safe_provider_id(provider_id):
        raise ValueError("invalid provider id")
    namespace = dict(load_settings().provider_settings.get(provider_id, {}))
    namespace.pop(_EXTENSION_LAUNCH_KEY, None)
    return _safe_json(namespace)


def set_provider_setting(provider_id: str, key: str, value: Any) -> None:
    """Set one provider-specific value without touching other namespaces."""
    if not _safe_provider_id(provider_id):
        raise ValueError("invalid provider id")
    if (
        type(key) is not str
        or _SETTING_KEY_RE.fullmatch(key) is None
        or _is_sensitive_key(key)
        or key == _EXTENSION_LAUNCH_KEY
    ):
        raise ValueError("invalid or sensitive provider setting key")
    safe_value = _safe_json(value)
    with _file_lock():
        settings, launches = _write_state_unlocked()
        namespaces = _provider_settings(settings.provider_settings, strict=True)
        namespace = dict(namespaces.get(provider_id, {}))
        namespace[key] = safe_value
        namespaces[provider_id] = namespace
        candidate = asdict(settings)
        candidate["provider_settings"] = namespaces
        normalised = _normalise(candidate, strict=True)
        _save_unlocked(_with_current_launches(normalised, launches))


def clear_provider_settings(provider_id: str) -> bool:
    """Remove ordinary provider values without clearing Core launch state."""
    if not _safe_provider_id(provider_id):
        raise ValueError("invalid provider id")
    with _file_lock():
        settings, launches = _write_state_unlocked()
        namespace = settings.provider_settings.get(provider_id)
        if namespace is None:
            return False
        ordinary = dict(namespace)
        ordinary.pop(_EXTENSION_LAUNCH_KEY, None)
        if not ordinary:
            return False
        settings.provider_settings.pop(provider_id, None)
        _save_unlocked(_with_current_launches(settings, launches))
        return True


def get_extension_launch_settings(
    provider_id: str,
) -> Optional[ExtensionLaunchSettingsV1]:
    """Return the typed receipt pointer reserved by Core, if configured."""

    if not _safe_provider_id(provider_id):
        raise ValueError("invalid provider id")
    data = _read_payload()
    if data is None:
        try:
            settings_file_exists = SETTINGS_FILE.exists()
        except OSError:
            settings_file_exists = True
        if settings_file_exists:
            raise ValueError("extension launch settings could not be read")
        return None
    version = data.get("version")
    inner = data.get("settings")
    if version == _LEGACY_VERSION and type(inner) is dict:
        return None
    if type(version) is not int or version != _VERSION or type(inner) is not dict:
        raise ValueError("extension launch settings document is invalid")
    namespaces = inner.get("provider_settings", {})
    if type(namespaces) is not dict:
        raise ValueError("extension launch settings namespace is invalid")
    if provider_id not in namespaces:
        return None
    namespace = namespaces[provider_id]
    if type(namespace) is not dict:
        raise ValueError("extension launch settings namespace is invalid")
    if _EXTENSION_LAUNCH_KEY not in namespace:
        return None
    return _extension_launch_record(
        namespace[_EXTENSION_LAUNCH_KEY], provider_id=provider_id,
    )


def set_extension_launch_settings(value: ExtensionLaunchSettingsV1) -> None:
    """Atomically replace only Core's reserved provider launch pointer."""

    record = _extension_launch_json(value)
    provider_id = value.provider_id
    with _file_lock():
        data = _read_payload()
        version = data.get("version") if data else None
        inner = (
            data.get("settings")
            if data and type(version) is int
            and version in (_LEGACY_VERSION, _VERSION)
            else {}
        )
        if version == _LEGACY_VERSION and type(inner) is dict:
            inner = {
                "lang": inner.get("lang"),
                "default_provider": inner.get("default_provider"),
            }
        settings = _normalise(inner if type(inner) is dict else {}, strict=False)
        namespaces = _provider_settings(settings.provider_settings, strict=True)
        namespace = dict(namespaces.get(provider_id, {}))
        namespace[_EXTENSION_LAUNCH_KEY] = record
        namespaces[provider_id] = namespace
        candidate = asdict(settings)
        candidate["provider_settings"] = namespaces
        _save_unlocked(_normalise(candidate, strict=True))


def clear_extension_launch_settings(provider_id: str) -> bool:
    """Remove only Core's reserved launch pointer for one provider."""

    if not _safe_provider_id(provider_id):
        raise ValueError("invalid provider id")
    with _file_lock():
        data = _read_payload()
        version = data.get("version") if data else None
        inner = (
            data.get("settings")
            if data and type(version) is int
            and version in (_LEGACY_VERSION, _VERSION)
            else {}
        )
        if version == _LEGACY_VERSION and type(inner) is dict:
            inner = {
                "lang": inner.get("lang"),
                "default_provider": inner.get("default_provider"),
            }
        raw_namespace = None
        if type(inner) is dict:
            raw_namespaces = inner.get("provider_settings")
            if type(raw_namespaces) is dict:
                candidate_namespace = raw_namespaces.get(provider_id)
                if type(candidate_namespace) is dict:
                    raw_namespace = candidate_namespace
        had_reserved_value = (
            raw_namespace is not None
            and _EXTENSION_LAUNCH_KEY in raw_namespace
        )
        settings = _normalise(inner if type(inner) is dict else {}, strict=False)
        namespaces = _provider_settings(settings.provider_settings, strict=True)
        namespace = dict(namespaces.get(provider_id, {}))
        if not had_reserved_value and _EXTENSION_LAUNCH_KEY not in namespace:
            return False
        namespace.pop(_EXTENSION_LAUNCH_KEY, None)
        if namespace:
            namespaces[provider_id] = namespace
        else:
            namespaces.pop(provider_id, None)
        candidate = asdict(settings)
        candidate["provider_settings"] = namespaces
        _save_unlocked(_normalise(candidate, strict=True))
        return True


__all__ = [
    "ExtensionLaunchSettingsV1", "Settings", "SETTINGS_DIR", "SETTINGS_FILE",
    "load_settings", "save_settings",
    "get", "set", "get_provider_settings", "set_provider_setting",
    "clear_provider_settings", "get_extension_launch_settings",
    "set_extension_launch_settings", "clear_extension_launch_settings",
]
