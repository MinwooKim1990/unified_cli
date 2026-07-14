"""Persistent user settings for the CLI/REPL.

Small key/value store at `~/.unified-cli/settings.json` (same dir as
`state.py`). Currently holds the UI language preference; kept separate from
`SessionState` because it is a durable preference, not per-turn chat state.

Mirrors `state.py`'s atomic-write + silent-fallback pattern intentionally.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

SETTINGS_DIR = Path.home() / ".unified-cli"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"
_VERSION = 1
_PROVIDERS = frozenset(("claude", "codex", "gemini"))


@dataclass
class Settings:
    lang: Optional[str] = None              # None → autodetect/default at use site
    default_provider: Optional[str] = None  # None → Claude compatibility default


def _ensure_dir() -> None:
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    """Return saved settings, or defaults on any missing/invalid file.

    Silent fallback on any I/O or parse error — this is a convenience file.
    """
    try:
        with SETTINGS_FILE.open(encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return Settings()

    if not isinstance(data, dict) or data.get("version") != _VERSION:
        return Settings()
    inner = data.get("settings")
    if not isinstance(inner, dict):
        return Settings()
    default_provider = inner.get("default_provider")
    if not isinstance(default_provider, str) or default_provider not in _PROVIDERS:
        default_provider = None
    return Settings(lang=inner.get("lang"), default_provider=default_provider)


def save_settings(s: Settings) -> None:
    """Atomically overwrite the settings file."""
    _ensure_dir()
    payload = {"version": _VERSION, "settings": asdict(s)}

    # Atomic write: temp file in same dir → fsync → rename.
    fd, tmp_path = tempfile.mkstemp(prefix=".settings.", suffix=".json", dir=SETTINGS_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, SETTINGS_FILE)
        try:
            os.chmod(SETTINGS_FILE, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


def get(key: str, default: Any = None) -> Any:
    """Read one setting (load-on-read; fine for low-frequency CLI use)."""
    return getattr(load_settings(), key, default)


def set(key: str, value: Any) -> None:  # noqa: A001 - deliberate get/set pair
    """Update one setting (load-modify-save)."""
    s = load_settings()
    if not hasattr(s, key):
        raise KeyError(key)
    if (key == "default_provider" and value is not None
            and (not isinstance(value, str) or value not in _PROVIDERS)):
        raise ValueError(
            "default_provider must be one of: " + ", ".join(sorted(_PROVIDERS))
        )
    setattr(s, key, value)
    save_settings(s)
