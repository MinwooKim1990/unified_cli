"""Shared test fixtures.

Make i18n deterministic regardless of the developer's machine: isolate the
settings file to a temp dir and clear UNIFIED_CLI_LANG, so the resolved
language defaults to English unless a test sets it explicitly. Without this,
a real ~/.unified-cli/settings.json (e.g. lang: ko) would leak into tests that
assert on user-facing strings.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unified_cli import i18n
from unified_cli import settings as _settings


@pytest.fixture(autouse=True)
def _isolate_i18n(tmp_path, monkeypatch):
    d = tmp_path / ".unified-cli-conftest"
    monkeypatch.setattr(_settings, "SETTINGS_DIR", d)
    monkeypatch.setattr(_settings, "SETTINGS_FILE", d / "settings.json")
    monkeypatch.delenv("UNIFIED_CLI_LANG", raising=False)
    i18n.set_lang(None)
    yield
    i18n.set_lang(None)
