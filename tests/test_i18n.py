"""Tests for the i18n layer (en default, ko fallback, total t())."""

from __future__ import annotations

import sys
import ast
import string
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


def test_message_catalogs_have_identical_keys():
    assert i18n.MESSAGES["ko"].keys() == i18n.MESSAGES["en"].keys()


def _fields(template):
    return {
        name.split(".", 1)[0].split("[", 1)[0]
        for _literal, name, _spec, _conversion in string.Formatter().parse(template)
        if name
    }


def test_message_catalogs_have_identical_placeholders():
    for key, english in i18n.MESSAGES["en"].items():
        assert _fields(english) == _fields(i18n.MESSAGES["ko"][key]), key


def test_catalog_source_has_no_duplicate_literal_keys():
    source = Path(i18n.__file__).read_text(encoding="utf-8")
    module = ast.parse(source)
    catalogs = {
        node.targets[0].id: node.value
        for node in module.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"_EN", "_KO"}
    }
    for name, value in catalogs.items():
        assert isinstance(value, ast.Dict)
        keys = [key.value for key in value.keys if isinstance(key, ast.Constant)]
        assert len(keys) == len(set(keys)), name


def test_stage3_korean_messages_are_localized():
    i18n.set_lang("ko")
    assert "권한" in i18n.t("repl.permissions.confirm", old="read_only", new="workspace_write")
    assert "추론 요약" in i18n.t("repl.renderer.reasoning_summary", text="safe")
    assert "추가 디렉터리" in i18n.t("repl.add_dir.added")
