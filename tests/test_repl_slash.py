"""Logic tests for the REPL slash dispatcher (no live prompt / terminal)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unified_cli import i18n
from unified_cli import settings as st
from unified_cli import repl
from unified_cli.conversation import UnifiedConversation
from unified_cli.models import DEFAULT_MODELS


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.delenv("UNIFIED_CLI_ENABLE_GEMINI", raising=False)
    monkeypatch.delenv("UNIFIED_CLI_LANG", raising=False)
    d = tmp_path / ".unified-cli"
    monkeypatch.setattr(st, "SETTINGS_DIR", d)
    monkeypatch.setattr(st, "SETTINGS_FILE", d / "settings.json")
    i18n.set_lang(None)
    yield
    i18n.set_lang(None)


def _fresh():
    conv = UnifiedConversation(default_provider="claude", sticky=False)
    current = {"provider": "claude", "model": DEFAULT_MODELS["claude"]}
    return conv, current


def _slash(line, conv, current):
    return repl._handle_slash(line, conv, current, {}, [], use_ptk=False)


def test_exit_returns_true():
    conv, current = _fresh()
    assert _slash("/exit", conv, current) is True
    assert _slash("/quit", conv, current) is True


def test_unknown_returns_false():
    conv, current = _fresh()
    assert _slash("/nope", conv, current) is False


def test_model_with_arg_sets_model():
    conv, current = _fresh()
    _slash("/model claude-opus-4-7", conv, current)
    assert current["model"] == "claude-opus-4-7"


def test_provider_switch_sets_default_model():
    conv, current = _fresh()
    _slash("/provider codex", conv, current)
    assert current["provider"] == "codex"
    assert current["model"] == DEFAULT_MODELS["codex"]


def test_provider_gemini_locked_does_not_switch():
    conv, current = _fresh()
    _slash("/provider gemini", conv, current)
    assert current["provider"] == "claude"  # blocked by gate


def test_provider_gemini_unlocked_switches(monkeypatch):
    monkeypatch.setenv("UNIFIED_CLI_ENABLE_GEMINI", "1")
    conv, current = _fresh()
    _slash("/provider gemini", conv, current)
    assert current["provider"] == "gemini"


def test_lang_persists_and_flips():
    conv, current = _fresh()
    assert i18n.current_lang() == "en"
    _slash("/lang ko", conv, current)
    assert i18n.current_lang() == "ko"
    assert st.get("lang") == "ko"


def test_lang_unknown_rejected():
    conv, current = _fresh()
    _slash("/lang fr", conv, current)
    assert i18n.current_lang() == "en"  # unchanged


def test_new_clears_conversation():
    conv, current = _fresh()
    conv.turns.append(object())
    _slash("/new", conv, current)
    assert conv.turns == []


def test_image_not_found_is_handled(tmp_path):
    conv, current = _fresh()
    pending: list = []
    repl._handle_slash("/image /no/such/file.png", conv, current, {}, pending, use_ptk=False)
    assert pending == []


def test_image_attaches_existing(tmp_path):
    conv, current = _fresh()
    img = tmp_path / "x.png"
    img.write_bytes(b"x")
    pending: list = []
    repl._handle_slash(f"/image {img}", conv, current, {}, pending, use_ptk=False)
    assert len(pending) == 1
