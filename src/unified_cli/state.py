"""CLI-level session state persistence.

Stores the most recent `chat`/`repl` turn's (provider, model, session_id) so
`unified-cli chat "..." --continue` can resume without the user remembering a
UUID. File: `~/.unified-cli/state.json`.

Intentionally scoped to the CLI layer — BaseProvider / UnifiedConversation do
NOT touch this file. Python API users manage their own history explicitly.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from .core import ProviderName


STATE_DIR = Path.home() / ".unified-cli"
STATE_FILE = STATE_DIR / "state.json"
_VERSION = 1


@dataclass
class SessionState:
    provider: ProviderName
    model: str
    session_id: str
    cwd: str = ""
    updated_at: float = 0.0

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.updated_at) if self.updated_at else 0.0


def _ensure_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def save_last_session(
    provider: ProviderName,
    model: str,
    session_id: str,
    cwd: Optional[str] = None,
) -> None:
    """Atomically overwrite the last-session record."""
    _ensure_dir()
    state = SessionState(
        provider=provider, model=model, session_id=session_id,
        cwd=cwd or os.getcwd(), updated_at=time.time(),
    )
    payload = {"version": _VERSION, "last_session": asdict(state)}

    # Atomic write: temp file in same dir → fsync → rename.
    fd, tmp_path = tempfile.mkstemp(
        prefix=".state.", suffix=".json", dir=STATE_DIR,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


def load_last_session() -> Optional[SessionState]:
    """Return the saved last-session record, or None if missing/invalid.

    Silent fallback on any I/O or parse error — this is a convenience file,
    not a source of truth.
    """
    try:
        with STATE_FILE.open(encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None

    if not isinstance(data, dict) or data.get("version") != _VERSION:
        return None
    inner = data.get("last_session")
    if not isinstance(inner, dict):
        return None
    try:
        return SessionState(
            provider=inner["provider"],
            model=inner["model"],
            session_id=inner["session_id"],
            cwd=inner.get("cwd", ""),
            updated_at=float(inner.get("updated_at", 0.0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


def clear_last_session() -> bool:
    """Remove the state file. Returns True if something was removed."""
    try:
        STATE_FILE.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False
