"""Security regression tests for the legacy v1 last-session file."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unified_cli import state as state  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path):
    directory = tmp_path / ".unified-cli"
    with patch.multiple(state, STATE_DIR=directory, STATE_FILE=directory / "state.json"):
        yield


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
def test_save_and_load_use_private_modes():
    state.save_last_session("claude", "model", "session-1", cwd="/x")
    assert stat.S_IMODE(state.STATE_DIR.stat().st_mode) == 0o700
    assert stat.S_IMODE(state.STATE_FILE.stat().st_mode) == 0o600
    os.chmod(state.STATE_DIR, 0o755)
    os.chmod(state.STATE_FILE, 0o644)
    assert state.load_last_session() is not None
    assert stat.S_IMODE(state.STATE_DIR.stat().st_mode) == 0o700
    assert stat.S_IMODE(state.STATE_FILE.stat().st_mode) == 0o600


def test_oversized_and_untrusted_records_fail_closed():
    state.STATE_DIR.mkdir()
    state.STATE_FILE.write_bytes(b" " * (state._MAX_FILE_BYTES + 1))
    assert state.load_last_session() is None
    state.STATE_FILE.write_text(json.dumps({
        "version": 1,
        "last_session": {
            "provider": "../../bad", "model": "x", "session_id": "safe",
            "cwd": "", "updated_at": 1,
        },
    }), encoding="utf-8")
    assert state.load_last_session() is None
    state.STATE_FILE.write_text(json.dumps({
        "version": True,
        "last_session": {
            "provider": "claude", "model": "x", "session_id": "safe",
            "cwd": "", "updated_at": 1,
        },
    }), encoding="utf-8")
    assert state.load_last_session() is None


def test_opaque_session_reference_roundtrips_without_path_interpretation():
    opaque = "../provider/session name:한글"
    state.save_last_session("claude", "model", opaque, cwd="/x")
    assert state.load_last_session().session_id == opaque


def test_symlink_file_is_not_followed(tmp_path):
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    outside = tmp_path / "outside.json"
    outside.write_text("sentinel", encoding="utf-8")
    state.STATE_DIR.mkdir()
    try:
        state.STATE_FILE.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert state.load_last_session() is None
    state.save_last_session("claude", "model", "session-1", cwd="/x")
    assert outside.read_text(encoding="utf-8") == "sentinel"
    assert not state.STATE_FILE.is_symlink()


def test_replace_failure_preserves_old_file_and_cleans_temp(monkeypatch):
    state.save_last_session("claude", "model", "one", cwd="/x")
    original = state.STATE_FILE.read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("injected replace failure")

    monkeypatch.setattr(state.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        state.save_last_session("codex", "model", "two", cwd="/x")
    assert state.STATE_FILE.read_bytes() == original
    assert not any(path.name.startswith(".state.") for path in state.STATE_DIR.iterdir())


def test_unsafe_temp_target_is_rejected_without_touching_it(tmp_path, monkeypatch):
    outside = tmp_path / "outside.tmp"

    def unsafe_mkstemp(*_args, **_kwargs):
        fd = os.open(outside, os.O_RDWR | os.O_CREAT, 0o600)
        return fd, str(outside)

    monkeypatch.setattr(state.tempfile, "mkstemp", unsafe_mkstemp)
    with pytest.raises(OSError, match="state directory"):
        state.save_last_session("claude", "model", "one", cwd="/x")
    assert outside.exists()
    assert outside.read_bytes() == b""
