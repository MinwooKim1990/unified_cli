"""Unit tests for state.py — no network, no CLI subprocesses."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import unified_cli.state as state_mod  # noqa: E402
from unified_cli.state import (  # noqa: E402
    SessionState, clear_last_session, load_last_session, save_last_session,
)


def _with_tmp_state(tmp_path: Path):
    """Swap STATE_DIR/STATE_FILE to a temp dir."""
    tmp_dir = tmp_path / ".unified-cli"
    return patch.multiple(
        state_mod,
        STATE_DIR=tmp_dir,
        STATE_FILE=tmp_dir / "state.json",
    )


def test_load_missing_returns_none(tmp_path):
    with _with_tmp_state(tmp_path):
        assert load_last_session() is None


def test_save_then_load_roundtrip(tmp_path):
    with _with_tmp_state(tmp_path):
        save_last_session("claude", "claude-haiku-4-5", "abc-123", cwd="/x")
        got = load_last_session()
        assert got is not None
        assert got.provider == "claude"
        assert got.model == "claude-haiku-4-5"
        assert got.session_id == "abc-123"
        assert got.cwd == "/x"
        assert got.updated_at > 0


def test_clear_removes_file(tmp_path):
    with _with_tmp_state(tmp_path):
        save_last_session("codex", "gpt-5.4-mini", "t-1")
        assert load_last_session() is not None
        assert clear_last_session() is True
        assert load_last_session() is None
        # second clear is no-op
        assert clear_last_session() is False


def test_corrupt_json_returns_none(tmp_path):
    with _with_tmp_state(tmp_path):
        state_mod.STATE_DIR.mkdir(parents=True, exist_ok=True)
        state_mod.STATE_FILE.write_text("{ not valid json")
        assert load_last_session() is None


def test_version_mismatch_returns_none(tmp_path):
    with _with_tmp_state(tmp_path):
        state_mod.STATE_DIR.mkdir(parents=True, exist_ok=True)
        state_mod.STATE_FILE.write_text(json.dumps({
            "version": 99,
            "last_session": {"provider": "claude", "model": "x", "session_id": "y"},
        }))
        assert load_last_session() is None


def test_missing_required_field_returns_none(tmp_path):
    with _with_tmp_state(tmp_path):
        state_mod.STATE_DIR.mkdir(parents=True, exist_ok=True)
        state_mod.STATE_FILE.write_text(json.dumps({
            "version": 1,
            "last_session": {"provider": "claude"},  # no session_id
        }))
        assert load_last_session() is None


def test_save_overwrites_previous(tmp_path):
    with _with_tmp_state(tmp_path):
        save_last_session("claude", "haiku", "s1")
        save_last_session("gemini", "gemini-3.1-flash-lite-preview", "s2")
        got = load_last_session()
        assert got.provider == "gemini"
        assert got.session_id == "s2"


def test_atomic_write_leaves_no_temp_files(tmp_path):
    with _with_tmp_state(tmp_path):
        save_last_session("codex", "gpt-5.4-mini", "abc")
        files = list(state_mod.STATE_DIR.iterdir())
        # exactly state.json — no leftover .state.*.json tempfile
        assert [f.name for f in files] == ["state.json"]


def test_session_state_age_seconds():
    import time as _t
    s = SessionState(
        provider="claude", model="x", session_id="y", updated_at=_t.time() - 10
    )
    assert 9 <= s.age_seconds <= 12


if __name__ == "__main__":  # manual run
    import traceback, tempfile, shutil
    passed = failed = 0
    for name, fn in list(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        try:
            # fixture-style: pass tmp_path if signature has it
            import inspect
            sig = inspect.signature(fn)
            if "tmp_path" in sig.parameters:
                tmp = Path(tempfile.mkdtemp())
                try:
                    fn(tmp)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
            else:
                fn()
            passed += 1
            print(f"  ✓ {name}")
        except Exception:
            failed += 1
            print(f"  ✗ {name}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
