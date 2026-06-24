"""Tests for the i18n layer (en default, ko fallback, total t())."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unified_cli import i18n


@pytest.fixture(autouse=True)
def _reset_lang():
    i18n.set_lang(None)
    yield
    i18n.set_lang(None)


def test_default_is_english(monkeypatch):
    monkeypatch.delenv("UNIFIED_CLI_LANG", raising=False)
    monkeypatch.setattr("unified_cli.settings.get", lambda k, d=None: None)
    assert i18n.detect_lang() == "en"
    assert i18n.t("repl.exit.bye") == "bye."


def test_set_lang_overrides_everything(monkeypatch):
    monkeypatch.setenv("UNIFIED_CLI_LANG", "en")
    i18n.set_lang("ko")
    assert i18n.current_lang() == "ko"
    assert i18n.t("repl.exit.bye") == "bye."  # this key is same in both; check a diff one
    assert i18n.t("repl.new.done") == "대화 초기화됨."


def test_resolution_order_env_over_settings(monkeypatch):
    monkeypatch.setattr("unified_cli.settings.get", lambda k, d=None: None)
    monkeypatch.setenv("UNIFIED_CLI_LANG", "ko")
    assert i18n.detect_lang() == "ko"


def test_settings_over_env(monkeypatch):
    monkeypatch.setattr("unified_cli.settings.get", lambda k, d=None: "ko")
    monkeypatch.setenv("UNIFIED_CLI_LANG", "en")
    assert i18n.detect_lang() == "ko"


def test_set_lang_rejects_unknown():
    with pytest.raises(ValueError):
        i18n.set_lang("fr")


def test_missing_key_returns_key():
    assert i18n.t("this.key.does.not.exist") == "this.key.does.not.exist"


def test_bad_format_arg_returns_template():
    # repl.model.changed expects {model}; call without it → returns template, no raise
    out = i18n.t("repl.model.changed")
    assert "{model}" in out


def test_format_substitution():
    i18n.set_lang("en")
    assert i18n.t("repl.model.changed", model="opus") == "model changed: opus (same provider)"


def test_ko_falls_back_to_en_for_missing_key(monkeypatch):
    # Inject a key only present in en, then ask under ko.
    monkeypatch.setitem(i18n.MESSAGES["en"], "_test.only_en", "english-only")
    i18n.set_lang("ko")
    assert i18n.t("_test.only_en") == "english-only"


def test_every_ko_key_exists_in_en():
    missing = [k for k in i18n.MESSAGES["ko"] if k not in i18n.MESSAGES["en"]]
    assert not missing, f"ko keys missing from en: {missing}"
