"""Tests for the persistent settings store (atomic write, silent fallback)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unified_cli import settings as st


@pytest.fixture(autouse=True)
def _tmp_settings(tmp_path, monkeypatch):
    d = tmp_path / ".unified-cli"
    monkeypatch.setattr(st, "SETTINGS_DIR", d)
    monkeypatch.setattr(st, "SETTINGS_FILE", d / "settings.json")
    yield


def test_load_missing_returns_defaults():
    s = st.load_settings()
    assert s.lang is None
    assert s.default_provider is None


def test_roundtrip():
    st.save_settings(st.Settings(lang="ko"))
    assert st.load_settings().lang == "ko"


def test_set_get_convenience():
    st.set("lang", "en")
    assert st.get("lang") == "en"
    assert st.get("nope", "fallback") == "fallback"


def test_set_unknown_key_raises():
    with pytest.raises(KeyError):
        st.set("not_a_field", "x")


def test_corrupt_json_falls_back(monkeypatch):
    st.SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    st.SETTINGS_FILE.write_text("{ not valid json", encoding="utf-8")
    assert st.load_settings().lang is None  # no raise


def test_version_mismatch_falls_back():
    st.SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    st.SETTINGS_FILE.write_text('{"version": 999, "settings": {"lang": "ko"}}', encoding="utf-8")
    assert st.load_settings().lang is None


def test_atomic_write_leaves_no_temp_files():
    st.save_settings(st.Settings(lang="en"))
    leftovers = [p.name for p in st.SETTINGS_DIR.iterdir() if p.name.startswith(".settings.")]
    assert leftovers == []
