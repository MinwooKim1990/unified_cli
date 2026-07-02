"""Regression tests for the 0.3.0 connectivity / headless-daemon fixes:
binary discovery under a minimal PATH, tri-state Keychain probe, and headless
token recognition.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unified_cli import discovery, ui


# ---- discovery: well-known-location fallback when PATH is minimal ----

def _make_exe(p: Path) -> str:
    p.write_text("#!/bin/sh\necho hi\n")
    p.chmod(0o755)
    return str(p)


def test_find_claude_uses_fallback_when_not_on_path(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CLI_PATH", raising=False)
    monkeypatch.setattr(discovery.shutil, "which", lambda _: None)
    exe = _make_exe(tmp_path / "claude")
    monkeypatch.setattr(discovery, "_CLAUDE_FALLBACK_BINS", [str(tmp_path / "claude")])
    assert discovery.find_claude_bin() == exe


def test_find_codex_uses_fallback_when_not_on_path(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_CLI_PATH", raising=False)
    monkeypatch.setattr(discovery.shutil, "which", lambda _: None)
    exe = _make_exe(tmp_path / "codex")
    monkeypatch.setattr(discovery, "_CODEX_FALLBACK_BINS", [str(tmp_path / "codex")])
    assert discovery.find_codex_bin() == exe


def test_path_still_wins_over_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CLI_PATH", raising=False)
    monkeypatch.setattr(discovery.shutil, "which", lambda _: "/usr/bin/claude")
    # Fallback should not even be consulted when PATH resolves.
    monkeypatch.setattr(discovery, "_CLAUDE_FALLBACK_BINS", ["/nonexistent/claude"])
    assert discovery.find_claude_bin() == "/usr/bin/claude"


def test_first_executable_skips_nonexecutable(tmp_path):
    plain = tmp_path / "plain"
    plain.write_text("x")  # not executable
    good = _make_exe(tmp_path / "good")
    assert discovery._first_executable([str(plain), str(good)]) == good


# ---- keychain tri-state probe ----

class _R:
    def __init__(self, rc):
        self.returncode = rc


def _force_darwin(monkeypatch):
    monkeypatch.setattr(ui.sys, "platform", "darwin")


def test_keychain_present(monkeypatch):
    _force_darwin(monkeypatch)
    monkeypatch.setattr(ui.subprocess, "run", lambda *a, **k: _R(0))
    assert ui._keychain_status("claude") == "present"


def test_keychain_absent(monkeypatch):
    _force_darwin(monkeypatch)
    monkeypatch.setattr(ui.subprocess, "run", lambda *a, **k: _R(44))
    assert ui._keychain_status("claude") == "absent"


def test_keychain_blocked(monkeypatch):
    _force_darwin(monkeypatch)
    monkeypatch.setattr(ui.subprocess, "run", lambda *a, **k: _R(51))
    assert ui._keychain_status("claude") == "blocked"


def test_keychain_na_off_darwin(monkeypatch):
    monkeypatch.setattr(ui.sys, "platform", "linux")
    assert ui._keychain_status("claude") == "na"


# ---- collect_states: headless token recognition ----

def test_collect_states_recognizes_oauth_token(monkeypatch):
    monkeypatch.setattr(ui.sys, "platform", "linux")  # avoid keychain probe
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-123")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    states = {s.name: s for s in ui.collect_states()}
    claude = states["claude"]
    assert claude.has_token_env is True
    assert claude.has_oauth is True  # token counts as usable auth


def test_collect_states_no_token_no_files(monkeypatch, tmp_path):
    monkeypatch.setattr(ui.sys, "platform", "linux")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    # Point auth files at an empty dir so none exist.
    monkeypatch.setattr(ui, "_AUTH_FILES", {
        "claude": tmp_path / "c", "codex": tmp_path / "x", "gemini": tmp_path / "g",
    })
    claude = {s.name: s for s in ui.collect_states()}["claude"]
    assert claude.has_token_env is False


def test_auth_cell_shows_token_label(monkeypatch, tmp_path):
    monkeypatch.setattr(ui, "_AUTH_FILES", {"claude": tmp_path / "none"})
    st = ui.ProviderState(
        name="claude", bin_path="/x", has_oauth=True, has_api_key=False,
        api_key_env="ANTHROPIC_API_KEY", model_count=1, model_source="hardcoded",
        default_model="m", has_token_env=True, keychain="absent",
    )
    assert "Token" in str(ui.auth_cell(st))


def test_auth_cell_flags_blocked_keychain():
    st = ui.ProviderState(
        name="claude", bin_path="/x", has_oauth=False, has_api_key=False,
        api_key_env="ANTHROPIC_API_KEY", model_count=1, model_source="hardcoded",
        default_model="m", has_token_env=False, keychain="blocked",
    )
    from unified_cli.i18n import t
    assert t("ui.auth.keychain_blocked") in str(ui.auth_cell(st))
